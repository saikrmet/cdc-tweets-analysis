import os
import json
import logging
from datetime import datetime, timedelta, timezone
import azure.functions as func

from azure.identity import DefaultAzureCredential
from azure.core.credentials import AzureKeyCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlobServiceClient
from azure.search.documents.indexes import SearchIndexerClient
import tweepy
from chunking import chunk_text, generate_chunk_id

# Frozen now for timestamp
now = datetime.now()

def main(dailytimer: func.TimerRequest) -> None:
    """Function that pulls tweets about the Centers for Disease Control 
    and Prevention (CDC), format for AI Search index schema, push to blob 
    storage, and run indexer.

    Parameters:
        - dailytimer: trigger that defines how often to run function app

    Returns:
        - None
    """
    # Function variables

    # The number of total tweets pulled is equal to max_results x num_pages
    # Variable for number of tweets per page
    max_results = 5

    # Variable for number of pages
    num_pages = 1

    # Variable for number of tweets to add to index
    num_top_tweets = 5

    # Variable for number of tokens in each tweet chunk
    chunk_size = 512

    #Variable for number of token overlap between tweet chunks
    chunk_overlap = 50

    # Variable for the encoding model tiktoken will use to compute tokens
    encoding_model = "text-embedding-3-small"

    # --------------------------------------------------------------------------

    logging.info("Started CDC Tweets Ingestion Function App")

    credential = DefaultAzureCredential()

    keyvault_uri = os.environ["KEY_VAULT_URI"]
    keyvault_client = SecretClient(vault_url=keyvault_uri, 
                                   credential=credential)

    twitter_token = keyvault_client.get_secret("TWITTER-BEARER-TOKEN").value

    all_tweets = pull_tweets(twitter_token=twitter_token, max_results=max_results, 
                             num_pages=num_pages)
    
    logging.info("Pulled {} on {}.".format(len(all_tweets), 
                                        now.strftime("%B %d, %Y")))
    
    top_tweets = get_top_n_tweets(all_tweets=all_tweets, n=num_top_tweets)

    logging.info("Got top {} tweets".format(num_top_tweets))

    chunks = get_chunked_tweets(tweets=top_tweets, chunk_size=chunk_size, 
                                chunk_overlap=chunk_overlap, 
                                encoding_model=encoding_model)
    logging.info("Chunked top {} tweets into {} total chunks"
                 .format(num_top_tweets, len(chunks)))
    
    blob_url = keyvault_client.get_secret("TWEETS-BLOB-URL").value
    blob_container = keyvault_client.get_secret("TWEETS-BLOB-CONTAINER").value
    blob_path = "{}/cdc-chunks.json".format(now.date())

    blob_service = BlobServiceClient(account_url=blob_url, credential=credential)

    blob_client = blob_service.get_blob_client(container=blob_container, 
                                               blob=blob_path)
    blob_client.upload_blob(json.dumps(obj=chunks, indent=2), overwrite=True)

    logging.info("Uploaded {} chunks to path {}".format(len(chunks), blob_path))

    search_endpoint = keyvault_client.get_secret("SEARCH-ENDPOINT").value
    search_key = keyvault_client.get_secret("SEARCH-KEY").value
    indexer_name = keyvault_client.get_secret("SEARCH-INDEXER-NAME").value

    indexer_client = SearchIndexerClient(endpoint=search_endpoint, 
                                         credential=AzureKeyCredential(search_key))
    try:
        indexer_client.run_indexer(name=indexer_name)
        logging.info("Succesfully ran indexer {}".format(indexer_name))
    except Exception as e:
        logging.info("Error running indexer {}".format(indexer_name))


def get_chunked_tweets(tweets: list[str], chunk_size: int, 
                       chunk_overlap: int, encoding_model: str) -> list[str]:
    """Function that returns tweet chunks from the list of tweets

    Paramaters:
        - tweets: the list of tweets
        - chunk_size: the number of tokens per chunk
        - chunk_overlap: the number of token overlap between chunks of the
        same tweet
        - encoding_model: the name of the model used to compute tokens from text

    Returns:
        - list[str]: the list of chunks
    """
    # # For future enrichment 
    # hashtags_cleaned = []
    # media_urls_cleaned = []
    # entities = getattr(tweet, "entities", None)
    # if entities is not None:
    #     hashtags = getattr(entities, "hashtags", None)
    #     if hashtags is not None:
    #         # Get value of hashtag text object
    #         for h in hashtags:
    #             hashtags_cleaned.append(h.get("text"))
        
    #     media = getattr(entities, "media", None)
    #     if media is not None:
    #         for m in media:
    #             if m.get("type") == "photo":
    #                 media_urls_cleaned.append(m.get("media_url"))

    chunks = []
    for tweet in tweets:
        tweet_id = getattr(tweet, "id", None)
        if tweet_id is None:
            continue

        tweet_text = getattr(tweet, "text", None)
        if tweet_text is None:
            continue
        chunking_result = chunk_text(text=tweet.text, chunk_size=chunk_size,
                                           chunk_overlap=chunk_overlap, 
                                           encoding_model=encoding_model)

        for i, chunked_text in enumerate(chunking_result):
            chunks.append(
                {
                    "id": generate_chunk_id(id=tweet_id, text=chunked_text),
                    "text": chunked_text,
                    "chunk_index": i,
                    "created_at": tweet.created_at.isoformat(),
                    "author_id": tweet.author_id,
                    "conversation_id": getattr(tweet, "conversation_id", None),
                    "source_url": f"https://twitter.com/i/web/status/{tweet.id}",
                    "popularity_score": score_tweet(tweet),
                    "ingestion_date": now.isoformat()
                    # "hashtags": hashtags_cleaned,
                    # "media_urls": media_urls_cleaned
                }
            )

    return chunks

def pull_tweets(twitter_token: str, max_results: int, num_pages: int) -> list[str]:
    """Function that pulls (max_results x num_pages) tweets containing "CDC" 
    from the last 24 hours

    Paramaters:
        - twitter_token: bearer token for tweepy
        - max_results: the number of tweets to return per page
        - num_pages: the number of tweet pages to return

    Returns:
        - list[str]: list of tweets
    """
    twitter_client = tweepy.Client(bearer_token=twitter_token)

    # Logic for current day 
    start_time = now - timedelta(1)
    end_time = now

    paginator = tweepy.Paginator(
        twitter_client.search_recent_tweets,
        query="CDC -is:retweet lang:en",
        tweet_fields=["id", "text", "created_at", "author_id", "entities", 
                      "conversation_id", "public_metrics"], 
        start_time=start_time.isoformat(),
        end_time=end_time.isoformat(), 
        max_results=max_results
    )

    all_tweets = []
    for page in paginator:
        if not page.data:
            continue
        all_tweets.extend(page.data)

        # Ensures we do not surpass tweet limit
        if len(all_tweets) >= (max_results * num_pages):
            break

    return all_tweets


def score_tweet(tweet) -> int:
    """Function that calculates a tweet's popularity score using public 
    metrics of the tweet.

    Parameters:
        - tweet: the tweet object returned by twitter_client

    Returns:
        - int: popularity score or 0 if public_metrics does not exist
    """
    metrics = getattr(tweet, "public_metrics")

    # Metrics doesn't exist, default to score of 0
    if metrics is None:
        return 0

    score = metrics.get("like_count", 0) * 0.5 \
        + metrics.get("retweet_count", 0) * 1.0 \
        + metrics.get("quote_count", 0) * 0.3 \
        + metrics.get("reply_count", 0) * 0.2
    return score


def get_top_n_tweets(all_tweets: list[str], n: int) -> list[str]:
    """Gets the top n tweets sorted by their popularity score computed 
    using the tweet's public metrics.

    Parameters:
        - all_tweets: the list of all tweets returned by pull_tweets
        - n: the number of top tweets to return

    Returns:
        - list[str]: top n tweets based on popularity score
    """
    scored_all_tweets = sorted(iterable=all_tweets, key=score_tweet, 
                               reverse=True) 
    return scored_all_tweets[:n] 