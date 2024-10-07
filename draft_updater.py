import datetime
import pprint

import httplib2
import os
import random
import sys
import time
import yaml

from apiclient.discovery import build
from apiclient.errors import HttpError
from apiclient.http import MediaFileUpload
from oauth2client.client import flow_from_clientsecrets
from oauth2client.file import Storage
from oauth2client.tools import argparser, run_flow

DRAFT_VIDEOS_FILE = "draft_videos.yml"

# Explicitly tell the underlying HTTP transport library not to retry, since
# we are handling retry logic ourselves.
httplib2.RETRIES = 1

# Maximum number of times to retry before giving up.
MAX_RETRIES = 10

# Always retry when these exceptions are raised.
RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, IOError)

# Always retry when an apiclient.errors.HttpError with one of these status
# codes is raised.
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]

# The CLIENT_SECRETS_FILE variable specifies the name of a file that contains
# the OAuth 2.0 information for this application, including its client_id and
# client_secret. You can acquire an OAuth 2.0 client ID and client secret from
# the Google API Console at
# https://console.cloud.google.com/.
# Please ensure that you have enabled the YouTube Data API for your project.
# For more information about using OAuth2 to access the YouTube Data API, see:
#   https://developers.google.com/youtube/v3/guides/authentication
# For more information about the client_secrets.json file format, see:
#   https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
CLIENT_SECRETS_FILE = "client_secrets.json"

# This OAuth 2.0 access scope allows an application to upload files to the
# authenticated user's YouTube channel, but doesn't allow other types of access.
YOUTUBE_EDIT_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

# This variable defines a message to display if the CLIENT_SECRETS_FILE is
# missing.
MISSING_CLIENT_SECRETS_MESSAGE = """
WARNING: Please configure OAuth 2.0

To make this sample run you will need to populate the client_secrets.json file
found at:

   %s

with information from the API Console
https://console.cloud.google.com/

For more information about the client_secrets.json file format, please visit:
https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
""" % os.path.abspath(os.path.join(os.path.dirname(__file__),
                                   CLIENT_SECRETS_FILE))

VALID_PRIVACY_STATUSES = ("public", "private", "unlisted")


def get_authenticated_service():
    flow = flow_from_clientsecrets(CLIENT_SECRETS_FILE,
                                   scope=YOUTUBE_EDIT_SCOPE,
                                   message=MISSING_CLIENT_SECRETS_MESSAGE)

    storage = Storage("%s-oauth2.json" % sys.argv[0])
    credentials = storage.get()

    if credentials is None or credentials.invalid:
        credentials = run_flow(flow, storage, args)

    return build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION,
                 http=credentials.authorize(httplib2.Http()))



if __name__ == "__main__":
    argparser.add_argument("--vids", required=False,
                           help="YAML file with info on videos to upload")
    args = argparser.parse_args()

    if args.vids:
        if not os.path.exists(args.vids):
            exit("Please specify a valid file using the --vids= parameter.")
        draft_vids_file = args.vids
    else:
        draft_vids_file = DRAFT_VIDEOS_FILE

    with open(DRAFT_VIDEOS_FILE) as f:
        vids_load = yaml.safe_load(f)

    if not vids_load.get('videos'):
        exit("That file did not contain any video information.")

    youtube = get_authenticated_service()

    for vid in vids_load['videos']:
        if not vid.get('url'):
            print("The video URL wasn't given. Skipping.")
            continue

        # full_filename = f"{video_path}/{vid['filename']}"

        tags = None
        if vid.get('keywords'):
            tags = vid['keywords'].split(",")

        date_string = vid['publishAt']
        publishDateTime = datetime.datetime.strptime(date_string, '%Y-%m-%d %H:%M:%S')

        id = vid['url'].split("/")[-1]
        body = {
            "id": id,
            "snippet": {
                "defaultLanguage": "en",
                "title": vid['title'],
                "description": vid['description'],
                "tags": tags,
                "categoryId": vid['categoryId'],
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
                "publishAt": publishDateTime.astimezone().isoformat()
            },
        }

        request = youtube.videos().update(part="snippet,status,localizations", body=body)
        try:
            upload_response = request.execute()
        except HttpError as e:
            print("An HTTP error %d occurred:\n%s" % (e.resp.status, e.content))
            exit("Check that HTTP error.")

        print(f"{upload_response}")