"""
Lambda function to create OpenSearch index with KNN vector support
"""
import json
import os
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def get_opensearch_client():
    """
    Create OpenSearch client with AWS authentication
    """
    region = os.environ['REGION']
    endpoint = os.environ['OPENSEARCH_ENDPOINT']
    
    # Get AWS credentials
    credentials = boto3.Session().get_credentials()
    awsauth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        region,
        'es',
        session_token=credentials.token
    )
    
    # Create OpenSearch client
    client = OpenSearch(
        hosts=[{'host': endpoint, 'port': 443}],
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30
    )
    
    return client

def get_index_configuration():
    """
    Define the index configuration with KNN vector support.
    Uses FAISS with innerproduct space type, which is equivalent to
    cosine similarity when vectors are L2-normalised (standard for
    embedding models).
    """
    return {
        "settings": {
            "index": {
                "knn": True
            }
        },
        "mappings": {
            "properties": {
                "training_options_std_hash_key": {
                    "type": "keyword"
                },
                "product_key": {
                    "type": "keyword"
                },
                "locale": {
                    "type": "keyword"
                },
                "training_options_std": {
                    "type": "keyword"
                },
                "product_version": {
                    "type": "keyword"
                },
                "option_embedding": {
                    "type": "knn_vector",
                    "dimension": 64,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "innerproduct"
                    }
                }
            }
        }
    }

def create_index(client, index_name):
    """
    Create the OpenSearch index
    """
    logger.info(f"Creating index: {index_name}")
    
    # Check if index already exists
    if client.indices.exists(index=index_name):
        logger.info(f"Index {index_name} already exists")
        return {
            "statusCode": 200,
            "body": f"Index {index_name} already exists"
        }
    
    # Create the index
    index_body = get_index_configuration()
    response = client.indices.create(
        index=index_name,
        body=index_body
    )
    
    logger.info(f"Index created successfully: {json.dumps(response)}")
    
    return {
        "statusCode": 200,
        "body": f"Index {index_name} created successfully"
    }

def delete_index(client, index_name):
    """
    Delete the OpenSearch index (for cleanup)
    """
    logger.info(f"Deleting index: {index_name}")
    
    if not client.indices.exists(index=index_name):
        logger.info(f"Index {index_name} does not exist")
        return {
            "statusCode": 200,
            "body": f"Index {index_name} does not exist"
        }
    
    response = client.indices.delete(index=index_name)
    logger.info(f"Index deleted successfully: {json.dumps(response)}")
    
    return {
        "statusCode": 200,
        "body": f"Index {index_name} deleted successfully"
    }

def lambda_handler(event, context):
    """
    Lambda handler for CloudFormation custom resource
    """
    logger.info(f"Received event: {json.dumps(event)}")
    
    index_name = os.environ['INDEX_NAME']
    request_type = event.get('RequestType', 'Create')
    
    try:
        client = get_opensearch_client()
        
        if request_type == 'Create' or request_type == 'Update':
            result = create_index(client, index_name)
            
        elif request_type == 'Delete':
            # Optionally delete the index on stack deletion
            # Comment out if you want to preserve data
            result = delete_index(client, index_name)
            
        else:
            raise ValueError(f"Unknown request type: {request_type}")
        
        logger.info(f"Operation completed: {result}")
        return {
            'PhysicalResourceId': f'opensearch-index-{index_name}',
            'Data': {
                'IndexName': index_name,
                'Result': result['body']
            }
        }
        
    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        # Re-raise so the CDK Provider framework marks the custom resource
        # as FAILED and CloudFormation surfaces the real error.
        # Do NOT return {'Status': 'FAILED'} — the framework ignores that
        # field and treats any non-raising return as SUCCESS.
        raise
