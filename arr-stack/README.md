# Arr Stack (LAN, ready to run)

Request → **Seerr** → **Sonarr / Radarr** → **Prowlarr** (indexers, FlareSolverr) → **qBittorrent / SABnzbd** (forced through **Gluetun VPN**) → **Unpackerr** → library → **Jellyfin** (+ **Bazarr** subtitles). **Recyclarr** applies TRaSH Guides profiles, custom formats and naming. **Homepage** is the dashboard.

Folder layout follows TRaSH Guides (single `/data` mount in every container, so imports are hardlinks):

```
DATA_ROOT/torrents/{movies,tv}   usenet/{incomplete,complete/{movies,tv}}   media/{movies,tv}
```

## Run

```bash
cp .env.example .env     # set DATA_ROOT, CONFIG_ROOT (same disk as media), HOST_IP, PUID/PGID, TIMEZONE, VPN_*
python3 setup.py
```

`setup.py` generates API keys and passwords (written back to `.env`), creates folders, starts everything, then wires up via each app's API:

| Integration | What gets configured |
|---|---|
| qBittorrent | known admin password, save path `/data/torrents`, categories via Sonarr/Radarr |
| SABnzbd | dirs, `tv`/`movies` categories, host whitelist, your usenet server if `USENET_*` set |
| Sonarr / Radarr | root folders, qBittorrent + SABnzbd clients, TRaSH profiles + naming (Recyclarr) |
| Prowlarr | FlareSolverr proxy, Sonarr/Radarr app sync, indexers listed in `INDEXERS` |
| Bazarr | pre-linked to Sonarr and Radarr |
| Jellyfin | admin user, Movies and TV Shows libraries |
| Seerr | signed in with Jellyfin, Sonarr (WEB-1080p) and Radarr (HD Bluray + WEB) as defaults |
| Unpackerr | Sonarr + Radarr, watching `/data/torrents` |

Safe to re-run; use it again after fixing a failure it reports.

## Still yours to supply

- **Media sources**: indexer accounts (Prowlarr → Indexers, or `INDEXERS=` in `.env`) and/or a usenet provider (`USENET_*`). Nothing downloads without one.
- **VPN credentials** (required; clients never run outside the tunnel). Enable port forwarding at your provider if you want better torrent speeds.
- **Bazarr**: languages + subtitle providers (Settings → Languages / Providers).

## Notes

- LAN-only: each *arr app requires login except from local addresses. Don't expose these ports to the internet; for remote access use a VPN (Tailscale/WireGuard) or the repo's Traefik + Authentik stacks.
- Ports: Homepage 3000, Jellyfin 8096, Seerr 5055, Sonarr 8989, Radarr 7878, Prowlarr 9696, Bazarr 6767, qBittorrent 8200, SABnzbd 8100.
- Not included (add from `../full-download-vpn` if wanted): Plex, Lidarr/Readarr/Whisparr, Tdarr, Traefik/Authentik.
