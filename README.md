# Aaron YouTube Uploader

First of all, I stole most of this from other repos. I'm afraid I've lost where I got most of
since I was just frantically searching for a way to upload videos, though. If I figure out
where I got it, credit will be given!

# Summary

This project goes through a YAML file and pulls information about videos (filename, title, 
description, etc.) and uploads the videos with those details. The script `aaron_uploader.py` 
does the heavy lifting.

# Setup

You need to set up OAUTH2, which is much easier than you think it is. The complicated 
stuff is handled by the script.

The functionality of OAUTH2 is beyond the scope of this document (and the author's technical
capabilities!), but you have to authorize your client (the application we're writing) to use
the YouTube API before you can make the calls. Some wands are waved, and now users of the OAUTH2
client can upload to their accounts.

Here are the steps as of October, 2024.

## Create a Project and Enable the APIs

This is just establishing a container for your client.

1. Go to [this link](https://console.cloud.google.com/apis/api/youtube.googleapis.com),
log in, and agree to the ToS.
1. Create a new project, which is a logical container for your application. The link to do that 
should be right on the page you land on.
1. Make sure `Enabled APIs & services` is the focus on the left.
1. Click `ENABLE APIS AND SERVICES` near the top.
1. You wind up at the API Library page. Search for "youtube" and select `YouTube Data API v3`.
1. Click `ENABLE`. Some spinny stuff happens, then it's enabled.

## Set up the Consent Screen

This is that screen that says "This flashlight app wants access to your contacts". 

1. Click `OAuth consent screen` on the left. Select `External` then `Create`.
1. Give your app a name and select an email address from the dropdown.
1. Give a developer email address at the bottom. `Save and Continue`.
1. Select `ADD OR REMOVE SCOPES` and select the one that says `../auth/youtube` in the "Scope" column.
1. Click `Update` at the bottom. `Save and Continue`.
1. Click `+ ADD USERS` and put your YouTube account login in there. Click `Add`. Click `Add` again
to get out of that window.
1. `Save and Continue`, `BACK TO DASHBOARD`.

## Set Up an OAUTH2 Credential

This is the creds your client uses to auth to YouTube. It establishes that your client is legit and
can use the APIs.

1. Click `Credentials` on the left.
1. Click `+ CREATE CREDENTIALS` near the top. `OAUTH client ID`.
1. `Application Type` is "Desktop App".
1. Give the creds a name (something useful) and click `CREATE`.
1. A popup with the cred info shows up. Click on `DOWNLOAD JSON` and save it in this repo as
`client_secrets.json`.

OATH2 should be working.

## The Uploader

As with all Python-based anything, I suggest using a virtual environment. That's beyond the scope
here.

First, you need to set up your YAML file.

### `video_summary.yml`

This file declares what videos needs to be uploaded and with what characteristics they have. There is
a sample file included in the repo.

There is an optional `video_dir` at the top of the structure to tell the script where to look for the
video files. If you don't declare on here, the script looks in the current directory.

The `videos` section is the bread-and-butter. This is where the video file locations and snippets &
statuses are declared. There's no checking here at the moment, so all the directives listed are
required.

```
video_dir: PATH TO THE VIDEOS  ** OPTIONAL, default to PWD
videos:
  - filename: "<YOUR FILE.MOV>"
    title: "<THE TITLE OF YOUR VIDEO>"
    description: "<THE DESCRIPTION TEXT>"
    categoryId: "<YOUTUBE CATEGORY ID>"  ** Use "22" if you don't care which one it is
    keywords: "<CSV LIST OF TAGS>"
    privacyStatus: "<PRIVACY SETTING>" ** "public", "private", or "unlisted"
  - filename: "<ANOTHER FILE.MOV>"
...
  - filename: "<YET ANOTHER FILE.MOV>"
...
```

### Running the Uploader

Running it is the easiest thing you'll do today.

> python aaron_upload.py

This script is very expensive [from a quota standpoint](#Quotas). Like very. A

A couple things will happen.

First, the client will use the info in the `client_secrets.json` file to authenticate against the
YouTube authentication server. This is what you signed up for above. A browser window will pop up
asking you to authenticate to YouTube. Use the same credentials you used to sign up for OAuth2 above.

When all goes well, the authentication server will provide an access token. This access token is stored
in a file called `aaron_uploader.py-oauth2.json`. This is what's being used to identify the client 
to the resource server. This file is generated automatically.

Now that the client is fully authenticated and authorized, the uploads carry on. The response is printed
to screen when the upload finishes so you can see what it did. This is obviously begging to be put in
some sort of summary or sent to another process to do other stuff like putting the videos into a playlist.

## The Updater

As with all Python-based anything, I suggest using a virtual environment. That's beyond the scope
here.

This script will update the details of an already-uploaded video. It is much cheaper to run [from a quota standpoint](#Quotas)
than uploading a video from the API (like 50 points versus 1600!), so it's an option for systematically
publishing your videos. I've been uploading videos via the YouTube page or app and using this guy
to set the title, description, tags, and, most importantly to me, the publish date and time.

Set up the YAML file before you get started.

### `draft_videos.yml`

This file declares what videos needs to be updated with what characteristics. There is
a sample file included in the repo.

The `videos` section is the bread-and-butter. This is where the URL of the video and snippets &
statuses are declared. There's no checking here at the moment, so all the directives listed are
required.

```commandline
videos:
  - url: "<THE URL OF THE VIDEO>"
    title: "<THE TITLE OF YOUR VIDEO>"
    description: "<THE DESCRIPTION TEXT>"
    categoryId: "<YOUTUBE CATEGORY ID>"  ** Use "22" if you don't care which one it is
    keywords: "<CSV LIST OF TAGS>"
    publishAt: "<THE DATE TO PUBLISH THE VIDEO IN '%Y-%m-%d %H:%M:%S' format>"
  - filename: "<ANOTHER URL>"
  ...
  - filename: "<ANOTHER URL>"
  ...
```

*A Note about the Date and Time*: The script will take care of that whole timezone thing, so just set the
`publishAt` based on your local date and time. 

### Running the Updater

Just run the script.

> python draft_updater.py

A couple things will happen.

First, the client will use the info in the `client_secrets.json` file to authenticate against the
YouTube authentication server. This is what you signed up for above. A browser window will pop up
asking you to authenticate to YouTube. Use the same credentials you used to sign up for OAuth2 above.

When all goes well, the authentication server will provide an access token. This access token is stored
in a file called `draft_updater.py-oauth2.json`. This is what's being used to identify the client 
to the resource server. This file is generated automatically.

Now that the client is fully authenticated and authorized, the updates carry on. The response is printed
to screen when the upload finishes so you can see what it did. This is obviously begging to be put in
some sort of summary or sent to another process to do other stuff like putting the videos into a playlist.

## Quotas

YouTube has quotas on the queries, and you get 10,000 credits as a test project. That sounds like a lot, 
but the measure isn't per transaction or per connection. Each query type has a different value, and,
as you guessed, the good ones are expensive. For example, uploading a video cost 1600 credits!

See [this page](https://developers.google.com/youtube/v3/determine_quota_cost) for the full credit cost list.



