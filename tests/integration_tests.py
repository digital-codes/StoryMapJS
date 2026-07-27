"""Integration testing requiring access to S3

These tests require the Docker Compose stack to be running with LocalStack.

Run: docker compose up

The tests connect to LocalStack at http://localhost:4566 (the exposed port from Docker).
"""
import importlib
import json
import os
import sys
import pytest
import botocore
import boto3

# Override AWS_ENDPOINT_URL for tests running on host machine
# (Docker uses localstack:4566, but from host we need localhost:4566)
if 'localstack:4566' in os.environ.get('AWS_ENDPOINT_URL', ''):
    os.environ['AWS_ENDPOINT_URL'] = 'http://localhost:4566'
elif not os.environ.get('AWS_ENDPOINT_URL'):
    os.environ['AWS_ENDPOINT_URL'] = 'http://localhost:4566'

# Set up settings module
if not os.environ.get('FLASK_SETTINGS_MODULE', ''):
    os.environ['FLASK_SETTINGS_MODULE'] = 'storymap.core.settings'

settings_module = os.environ.get('FLASK_SETTINGS_MODULE')
try:
    importlib.import_module(settings_module)
except ImportError as e:
    raise ImportError(f"Could not import settings '{settings_module}' (Is it on sys.path?): {e}")

settings = sys.modules[os.environ['FLASK_SETTINGS_MODULE']]
settings.TEST_MODE = False
# Tests run on the host, but settings default the DB host to the Docker-internal
# name "pg". Rewrite it to localhost (the port is published to the host).
try:
    if settings.DATABASES['pg']['HOST'] == 'pg':
        settings.DATABASES['pg']['HOST'] = 'localhost'
except (AttributeError, KeyError, TypeError):
    pass
# Use the actual bucket from settings (uploads.knilab.com) instead of a test bucket
# The bucket should be created by running scripts/makebuckets.sh
if not os.environ.get('AWS_TEST_BUCKET'):
    # Use the existing bucket from .env settings
    pass
else:
    settings.AWS_STORAGE_BUCKET_NAME = os.environ['AWS_TEST_BUCKET']

# Import storage module AFTER setting environment
from storymap.storage import all_keys, save_from_data
from storymap.connection import (pg_conn, create_user, get_user,
    set_user_storymap_value, set_user_migrated, delete_user_storymap)


@pytest.fixture
def pg_user():
    """Create a throwaway user in Postgres and clean it up afterwards."""
    uid = 'itest-concurrent-writes'
    db = pg_conn(settings)
    with db.cursor() as cursor:
        cursor.execute('DELETE FROM users WHERE uid=%s', (uid,))
    db.commit()
    create_user(uid, 'Integration Test', db=db, storymaps={
        'map-a': {'id': 'map-a', 'title': 'A', 'draft_on': 't0', 'published_on': ''},
        'map-b': {'id': 'map-b', 'title': 'B', 'draft_on': 't0', 'published_on': ''},
    })
    yield uid, db
    with db.cursor() as cursor:
        cursor.execute('DELETE FROM users WHERE uid=%s', (uid,))
    db.commit()
    db.close()


@pytest.mark.integration
def test_set_user_storymap_value_whole_key(pg_user):
    """Writing a whole storymap entry must not disturb sibling entries."""
    uid, db = pg_user
    set_user_storymap_value(uid, ['map-c'],
        {'id': 'map-c', 'title': 'C', 'draft_on': 't1', 'published_on': ''}, db=db)
    maps = get_user(uid, db=db)['storymaps']
    assert set(maps) == {'map-a', 'map-b', 'map-c'}
    assert maps['map-c']['title'] == 'C'
    assert maps['map-a']['title'] == 'A'  # untouched


@pytest.mark.integration
def test_set_user_storymap_value_single_field(pg_user):
    """Writing one field must leave the entry's other fields and siblings intact."""
    uid, db = pg_user
    set_user_storymap_value(uid, ['map-a', 'published_on'], 't2', db=db)
    maps = get_user(uid, db=db)['storymaps']
    assert maps['map-a']['published_on'] == 't2'
    assert maps['map-a']['title'] == 'A'      # other field intact
    assert maps['map-b']['published_on'] == ''  # sibling intact


@pytest.mark.integration
def test_delete_user_storymap_removes_only_target(pg_user):
    """Delete must remove exactly one key, leaving the rest of the account."""
    uid, db = pg_user
    delete_user_storymap(uid, 'map-a', db=db)
    maps = get_user(uid, db=db)['storymaps']
    assert set(maps) == {'map-b'}


@pytest.mark.integration
def test_set_user_migrated_leaves_storymaps_intact(pg_user):
    """Flipping the migrated flag must not rewrite the storymaps column."""
    uid, db = pg_user
    set_user_migrated(uid, 1, db=db)
    user = get_user(uid, db=db)
    assert user['migrated'] == 1
    assert set(user['storymaps']) == {'map-a', 'map-b'}


@pytest.mark.integration
def test_list_keys():
    """Test listing all S3 keys."""
    keys = all_keys()
    # TODO: this is not yet testing anything - add assertions
    assert keys is not None


@pytest.mark.integration
def test_save_from_data():
    """Test saving data to S3 and retrieving it."""
    file_name = 'test1.json'
    key_name = f'{settings.AWS_STORAGE_BUCKET_KEY}/{file_name}'
    content_type = 'application/json'
    content = json.dumps({'test_key': 'test_value'})

    # Save the data
    save_from_data(key_name, content_type, content)

    # Retrieve and verify
    # Use localhost instead of Docker hostname for tests running on host
    endpoint = settings.AWS_ENDPOINT_URL
    if endpoint and 'localstack:4566' in endpoint:
        endpoint = 'http://localhost:4566'

    if endpoint:
        s3 = boto3.resource('s3', endpoint_url=endpoint)
    else:
        s3 = boto3.resource('s3')

    obj = s3.Object(settings.AWS_STORAGE_BUCKET_NAME, key_name)

    try:
        retrieved_data = json.loads(obj.get()['Body'].read())
        assert retrieved_data['test_key'] == 'test_value'
    except botocore.exceptions.ConnectionError:
        pytest.fail("""
boto3 connection error in test. Check your environment variables

Be sure AWS_ENDPOINT_URL points to a valid localized endpoint. If connecting to S3, be sure AWS_ENDPOINT_URL is blank or not set and that AWS_SECRET_ACCESS_KEY and AWS_ACCESS_KEY_ID are set
""")
    except Exception as e:
        error_msg = str(e)
        if 'NoSuchBucket' in error_msg:
            pytest.fail(f"""
Could not connect. No such bucket: {obj.bucket_name}
AWS endpoint: {endpoint}

NOTE: StoryMap and these tests do not create the storage bucket. For testing, your endpoint should have a bucket named according to your AWS_TEST_BUCKET environment variable. With localstack, this bucket can be created with the following command:

aws --endpoint-url=http://localhost:4566 s3 mb s3://{settings.AWS_TEST_BUCKET}
""")
        elif 'NoSuchKey' in error_msg:
            pytest.fail("""
No such key error

The `save_from_data` function currently only saves to remote S3. To get this test passing, we will need to migrate to boto3 usage that allows for local storage (via localstack) or remote (to s3)
""")
        else:
            raise
