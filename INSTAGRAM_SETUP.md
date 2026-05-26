# Instagram setup (Part 3)

This guide connects recipe-kit to Instagram so people can **share a reel into
your DMs** and have it turned into a recipe automatically.

**Be honest with yourself up front:** this part takes about an hour and involves
clicking around Meta's developer dashboard, which is genuinely fiddly and changes
its layout from time to time. It's the same hassle for everyone — it's Meta, not
this kit. Parts 1 and 2 work fine without ever doing this. Only do it if you
actually want the "DM us a reel" feature.

> Tip: if you use Claude Code, you can paste this whole file to your agent and
> say *"walk me through this one step at a time."*

## What you'll end up with

Four secret values that you'll put in a `.env` file:

```
INSTAGRAM_PAGE_ACCESS_TOKEN=   # lets the app read shared reels and send replies
INSTAGRAM_VERIFY_TOKEN=        # a password you invent, to confirm the webhook
INSTAGRAM_APP_SECRET=          # confirms messages really came from Meta
INSTAGRAM_BUSINESS_ID=         # only needed for one token type (see step 5)
```

## Before you start

You need an **Instagram professional account** (Business or Creator — it's free
to switch in the Instagram app under *Settings → Account type*). A personal
account won't work for messaging.

## Step 1 — Make a Meta developer account

Go to <https://developers.facebook.com/>, log in with your Facebook account, and
accept the developer terms. (Yes, this needs a Facebook account even though it's
for Instagram.)

## Step 2 — Create an app

1. Click **My Apps → Create App**.
2. When asked what you want to build, choose the option for **Instagram** /
   messaging (Meta words this differently over time — look for "Instagram" or
   "Business"). Pick the **Business** app type if asked.
3. Give it any name. You now have an app.

## Step 3 — Add Instagram

In your app's dashboard, find **Add product** (or "Add use case") and add
**Instagram** with messaging. Connect your Instagram professional account when
it prompts you.

## Step 4 — Grab your App Secret

In the dashboard: **App settings → Basic**. Copy the **App Secret** (click Show).
That's your `INSTAGRAM_APP_SECRET`.

## Step 5 — Get an access token

In the Instagram messaging section there's a button to **generate an access
token** for your connected Instagram account. Generate it and copy it — that's
your `INSTAGRAM_PAGE_ACCESS_TOKEN`.

- If your token starts with **`IGAA`**, you're done — you do *not* need
  `INSTAGRAM_BUSINESS_ID`.
- If it starts with **`EAA`** (a Facebook-style token), you also need your
  Instagram account's numeric id for `INSTAGRAM_BUSINESS_ID`. The dashboard
  usually shows it next to the connected account; if not, your agent can fetch
  it for you.

> Tokens can expire. If replies suddenly stop working weeks later, regenerate
> the token here and update your `.env`.

## Step 6 — Put your server on the public internet

Meta needs to reach your webhook over **public HTTPS**. While testing on your own
computer, the easiest way is a tunnel:

1. Install [ngrok](https://ngrok.com/download) (free).
2. Start your service (from Part 1): `recipe-extractor-serve`
3. In another terminal: `ngrok http 8000`
4. ngrok prints a public address like `https://abc123.ngrok-free.app`. Your
   webhook URL is that address **+ `/instagram/webhook`**, e.g.
   `https://abc123.ngrok-free.app/instagram/webhook`.

(In real production you'd use your actual server's HTTPS domain instead of
ngrok.)

First, wire the webhook into your service. In the file where you start the app:

```python
from fastapi import FastAPI
from recipe_extractor import make_router

app = FastAPI()
app.include_router(make_router(auto_reply=True))   # GET/POST /instagram/webhook
```

## Step 7 — Tell Meta about your webhook

1. Invent a `INSTAGRAM_VERIFY_TOKEN` — any random word/phrase you like (e.g.
   `my-recipe-bot-9281`). Put it in your `.env`.
2. Set all four values in `.env`, then **restart** your service so it picks them
   up.
3. In the Meta dashboard, find the **Webhooks** section for Instagram. Enter:
   - **Callback URL:** your `https://…/instagram/webhook` from step 6
   - **Verify token:** the exact same word you chose above
4. Click verify/subscribe. Meta calls your server, your server echoes the
   challenge, and it turns green. (If it fails: the service isn't running, the
   URL is wrong, or the verify token doesn't match.)
5. Subscribe to the **`messages`** field (and message attachments if listed).

## Step 8 — Test it

From a *different* Instagram account, send your account a DM and **share a
cooking reel** into it. Within a few seconds you should see your service log the
event, extract the recipe, and (with `auto_reply=True`) reply with the recipe
title.

## Doing something with the recipe

By default it just confirms. To save the recipe into *your* database (or do
anything else), pass a function instead:

```python
def on_reel(sender_id, result):
    my_database.save(sender_id, result["recipe"], result["media"])
    return f"Saved {result['recipe']['title']} ✅"   # this gets DM'd back

app.include_router(make_router(on_reel=on_reel))
```

`result` is exactly what Part 1 produces — the recipe data plus the saved video.

## Troubleshooting

- **Webhook won't verify (stays red):** service not running, wrong callback URL,
  or the verify token in the dashboard doesn't match `INSTAGRAM_VERIFY_TOKEN`.
- **Verifies, but nothing happens on a DM:** make sure you subscribed to the
  `messages` field, and that you're sharing a *reel*, not a photo.
- **`bad_signature` in the logs:** `INSTAGRAM_APP_SECRET` is wrong or missing.
- **Replies don't send:** the access token is wrong/expired, or (EAA tokens
  only) `INSTAGRAM_BUSINESS_ID` is missing.
- **Local testing only:** Meta can't reach `localhost` — you must use the ngrok
  (or real HTTPS) URL, and re-running ngrok gives a new URL you'll need to update
  in the dashboard.

## Going live

Two things change for real users:
1. Swap the ngrok URL for your real server's HTTPS domain in the webhook config.
2. To accept DMs from accounts other than your own test ones, your Meta app has
   to go through Meta's **App Review** for Instagram messaging permissions. While
   your app is in "development mode" it only works with accounts you've added as
   testers — which is plenty for trying it out.
