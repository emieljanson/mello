# Track lists need your own Spotify app key

The list button on a cover shows every song in an album or playlist. Getting
that list requires Spotify's Web API, and the Web API rate-limits per client ID.

Albums and podcast shows need only the app key below. **Playlists additionally
need a one-time login** — see [Playlists also need a one-time
login](#playlists-also-need-a-one-time-login).

Mello can borrow an access token from go-librespot (`POST /token`) with no setup
at all — but that token carries go-librespot's client ID, which is shared by
every librespot, go-librespot and spotifyd install in the world. Its quota is
permanently exhausted. In practice every request comes back `429` on the first
try, with a `Retry-After` that never runs out:

```
Track list: fetching spotify:playlist:37i9dQZF1DZ06evO0lhGr6
Track list throttled (429): backing off 52s
```

Your own client ID gets its own quota, which one Pi will never come close to
using.

## Setup (about two minutes)

1. Open https://developer.spotify.com/dashboard and log in with your normal
   Spotify account.
2. **Create app**. Name and description can be anything ("Mello"). Redirect URI:
   `http://127.0.0.1:8080` — Spotify rejects `http://localhost` as insecure and
   only accepts the loopback IP literal over plain http. Mello never uses this
   value at all (client credentials has no redirect step), so any URI that
   passes their validation is fine. Tick the Web API checkbox.
3. Open the app's **Settings** to see the Client ID, then **View client secret**.
4. On the Pi, put both into `~/mello/.env`:

   ```bash
   nano ~/mello/.env
   ```

   ```
   SPOTIFY_CLIENT_ID=your_client_id_here
   SPOTIFY_CLIENT_SECRET=your_client_secret_here
   ```

5. Restart the app:

   ```bash
   sudo systemctl restart mello-native
   ```

Confirm it took:

```bash
grep -i "app token" ~/mello/mello.log | tail -3
```

`Spotify app token obtained (own client ID, own rate limit)` means you're set.
`Spotify app token rejected` means a typo in one of the two values.

That covers **albums and podcast shows**. Playlists need one more step.

## Playlists also need a one-time login

Spotify requires the `playlist-read-private` scope on
`/v1/playlists/{id}/tracks` — for *every* playlist, public ones included. The
app key above uses the *client credentials* flow, which carries no scopes at
all, so it can never read a playlist's tracks no matter who owns the playlist or
how public it is. Copying a playlist to make it yours does not help; that was
worth ruling out, and it doesn't.

So playlists need a real login, once:

```bash
python3 ~/mello/mello-login.py
```

Spotify only accepts a **loopback** redirect over plain http, so the browser you
log in with has to land on `127.0.0.1` *of the Pi*. Tunnel it from your laptop —
no browser or keyboard needed on the touchscreen:

```bash
ssh -N -L 8080:127.0.0.1:8080 mello@your-pi.local
```

Leave that running, then open the URL the script printed in your laptop's
browser, log in, press Agree. The script writes `SPOTIFY_REFRESH_TOKEN` into
`.env` and exits. Restart:

```bash
sudo systemctl restart mello-native
```

`Spotify login token refreshed (playlist track lists enabled)` in
`~/mello/mello.log` means you're set.

The redirect URI is the same `http://127.0.0.1:8080` you already registered — no
dashboard change needed. The scope is read-only and playlist-only: it cannot
control playback, see your history, or touch anything else. Playback still goes
entirely through go-librespot as before.

If the token is ever revoked, Mello logs it once and falls back to app-only
access — albums and shows keep working, playlists stop. Re-run the script.

## What still won't have a list

Spotify's own algorithmic and editorial playlists — Discover Weekly, Daily Mix,
Release Radar, "This Is …", most things with an ID starting `37i9dQZF1D` — are
closed to third-party apps. Requests for them come back `403` (or `404`) even
when you're logged in. Mello detects that, stops retrying, and says "Spotify
keeps its own playlists private". Copy such a mix into a playlist of your own
and add that instead.

The other case no login can fix is a playlist someone else made and never
shared with you — if it isn't visible in your own Spotify account, no app can
read it. Everything visible to your account works: your own playlists, private
or public, and playlists you've saved or followed from other people.

## Podcasts

Spotify's podcast pages have no show-level play button — you always tap an
individual episode, so the playback context Mello sees is
`spotify:episode:...`. Saved verbatim that gives one tile per episode.

So `+` resolves the episode's parent **show** and saves that instead: one tile,
every episode in the track list. Nothing to do differently — play any episode of
the podcast and press `+`.

If that lookup returns 404, episode availability is market-scoped and an app
token carries no country of its own. Add your country to `.env`:

```
SPOTIFY_MARKET=FR
```

Note that hand-picking episodes into a normal playlist does **not** work — the
Spotify app itself refuses to play those ("Spotify can't play this right now").
Add the show instead.

## Keeping the key

`.env` is gitignored, so an app update won't overwrite it. Nothing else needs
doing — the key and the refresh token are read at startup, and both tokens
refresh themselves hourly.
