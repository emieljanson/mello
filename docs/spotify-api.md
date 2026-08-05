# Track lists need your own Spotify app key

The list button on a cover shows every song in an album or playlist. Getting
that list requires Spotify's Web API, and the Web API rate-limits per client ID.

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

No user login or authorisation is involved. Mello uses the *client credentials*
flow, which only reads public catalog data — album tracks, playlist tracks, show
episodes. It cannot see your library, your history, or control playback. Playback
still goes entirely through go-librespot as before.

## What still won't have a list

Spotify's own algorithmic and editorial playlists — Discover Weekly, Daily Mix,
Release Radar, "This Is …", most things with an ID starting `37i9dQZF1D` — are
closed to third-party apps. Requests for them return `404` no matter whose key
you use. Mello detects that, stops retrying, and says "Spotify keeps its own
playlists private".

Albums, your own playlists, other people's public playlists and podcast shows
all work normally. If you want a list for a Spotify-curated mix, copy its tracks
into a playlist of your own and add that to Mello instead.

## Keeping the key

`.env` is gitignored, so an app update won't overwrite it. Nothing else needs
doing — the key is read at startup and the token refreshes itself hourly.
