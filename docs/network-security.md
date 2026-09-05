# Network security

The safest deployment is the default: the app listens on `127.0.0.1`, accepts
only loopback Host headers, and is reachable only from the same computer. No
firewall change or CORS setting is needed.

## Trusted LAN access

Treat every device on a LAN as a separate security principal. Set a long random
token before changing the listener:

```bash
openssl rand -hex 32
```

```dotenv
HOST=0.0.0.0
API_TOKEN=paste-the-random-value-here
```

`make start` refuses a non-loopback listener when `API_TOKEN` is empty. Enter
the same token in **Settings -> API access** on each browser. HTTP API calls use
the `X-API-Token` header; media and WebSocket connections use separate,
path-scoped `SameSite=Strict` cookies so the token never appears in a URL.

`CORS_ORIGINS` is only for a browser frontend hosted at a different exact
origin. List origins as comma-separated `scheme://host:port` values. Wildcards
are ignored because a tokenless wildcard would make arbitrary web pages callers
of a local API. Command-line clients may omit `Origin`; browser writes and every
WebSocket connection are checked against their served or configured origin.

Do not port-forward the Uvicorn listener or expose it directly to the internet.
For access outside one trusted LAN, keep the app on loopback, set `API_TOKEN`,
and put a maintained reverse proxy or private overlay network in front of it.
Terminate HTTPS there, preserve the original `Host` header, and use a hostname
and certificate trusted by every client. The application is not its own public
identity provider, rate limiter, or TLS terminator.

## WSL2 firewall helper

From an elevated Windows PowerShell in the checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows-lan-access.ps1
```

The helper creates only TCP/8000 rules for the Windows **Private** profile and
`LocalSubnet`. On mirrored-mode WSL it also creates a port-scoped Hyper-V rule;
it never changes the VM-wide default inbound action. Remove both named rules
when they are no longer needed:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows-lan-access.ps1 -Remove
```

Microsoft documents the [WSL networking modes and Hyper-V firewall example](https://learn.microsoft.com/en-us/windows/wsl/networking)
and the [Hyper-V firewall rule cmdlet](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallhypervrule?view=windowsserver2025-ps).
If the Hyper-V cmdlets are unavailable, the helper prints a warning and applies
no broad fallback.

## Metadata privacy

Turning off **Generation metadata** stops metadata from being added to new PNGs
and videos, suppresses video sidecars from static serving and ZIP exports, and
sets export-manifest generation data to `null`. The database keeps the private
record so the local library and rerun workflow continue to work. Existing PNG
metadata is not scrubbed; export or process old files separately if they need
retroactive redaction.
