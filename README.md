# P2 archive S3

Small on-server process for Excellent archive files. Higent's S3 tools call it. `GetObject` runs `ReadEncodedFile`. `PutObject` runs `WriteEncodedFile`. Presigned GET and PUT URLs are those same two calls with the signature in the query string.

The bucket is the division folder (`01003`). The object key ends in the document guid. Decoding stays inside the licensed program. This process loads it once, through PowerShell 7, and keeps the assemblies in memory.

```
Higent cloud                         Windows server
────────────                         ──────────────
S3 tool ──► Gateway Broker ◄── Gateway Agent ──► this service :9000
                                                      │
                                                      ▼
                                               ReadEncodedFile
                                               WriteEncodedFile
```

## Run

PowerShell 7 (`pwsh`) and the Excellent installation (`DMS.dll`, `Sph.Imhotep.Controller.General.dll`) are required. Windows PowerShell 5.1 cannot load the .NET 8 assemblies.

```powershell
$env:ARCHIVE_SHARE_ROOT = "F:\Shares\UNI-Daten\Share"
$env:ARCHIVE_APP_DIR = "C:\UNI-Programme\UNI-ExcellentP2"
$env:ARCHIVE_S3_ACCESS_KEY = "the-access-key"
$env:ARCHIVE_S3_SECRET_KEY = "the-secret"
$env:ARCHIVE_S3_REGION = "us-east-1"
$env:ARCHIVE_LISTEN_PORT = "9000"
python server.py
```

`GET /health` returns `ok` and does not touch the archive.

## Install as a service

From an elevated PowerShell, in this directory:

```powershell
.\install.ps1 `
  -ShareRoot "F:\Shares\UNI-Daten\Share" `
  -AppDir "C:\UNI-Programme\UNI-ExcellentP2" `
  -AccessKey "the-access-key" `
  -SecretKey "the-secret" `
  -Port 9000
```

That copies the files to `C:\ProgramData\higent\p2-archive-s3`, creates a virtualenv, and writes `HigentP2Archive.xml` in the same WinSW shape as the gateway connector. Copy the WinSW executable you already use to `HigentP2Archive.exe` beside that xml, then:

```powershell
C:\ProgramData\higent\p2-archive-s3\HigentP2Archive.exe install
C:\ProgramData\higent\p2-archive-s3\HigentP2Archive.exe start
```

The access key and secret are stored in the service xml, the same way the gateway enrollment token is stored for the connector.

## Point Higent at it

The service listens on `127.0.0.1`. Set `ARCHIVE_LISTEN_HOST=0.0.0.0` when the Gateway Agent runs on another machine, and allow that host and port on the Gateway.

1. On the Gateway, allow `127.0.0.1:9000` when the agent is on this machine.
2. In the S3 integration, set the endpoint to `http://127.0.0.1:9000`, the same access key, secret, and region, and select that Gateway.
3. Download Object and Upload Object then dial the endpoint through the Gateway. Generate Presigned URL does not dial. The URL names the endpoint host, and this service accepts it.

`WriteEncodedFile` stores the file through the installation's own archive path. The bucket selects the division folder for reads (`Excellent\01003\Archiv\{guid}`). Use the division this installation writes.
