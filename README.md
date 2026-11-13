# nas-s3-sync

Uploads NAS shared folders to the `envicomm-nas-datahub` S3 bucket and writes a redacted copy of
each file that contains PII at `redacted/<same path>`. This replaces dragging folders into S3 by
hand.

```
NAS 192.168.0.50  ──(CIFS, read-only)──>  this Linux PC  ──./sync.sh──>  s3://envicomm-nas-datahub/
  MPAL/...                                 /mnt/nas/MPAL                    MPAL/...
                                                                            redacted/MPAL/...
```

## What a run does

- Uploads only files whose key is **missing** in the bucket or whose **size changed**. Running
  it again uploads nothing new.
- **Never deletes** anything in the bucket. Removing a file on the NAS leaves the S3 copy.
- Uploads **every file type**. Redacted copies are made for:

  | Type | Redacted copy |
  |---|---|
  | `.pdf` | PDF with the text truly removed and black boxes drawn (PyMuPDF) |
  | `.docx`, `.xlsx` | `.txt` with the text extracted and redacted |
  | `.txt .csv .json .xml .yaml .yml .html .log .md .tsv` | same file, redacted |

  A copy is only written when something was found. Everything else (images, scanned PDFs,
  password-protected PDFs, files over `max_redact_mb`) is uploaded as-is and shows up as
  `not-redactable` in the report.
- Redacts emails, PH government IDs (TIN, SSS, PhilHealth), US SSNs and money amounts. Phone
  numbers are kept. The patterns are ported from the apo-turbo redaction Lambda.
- **Skips** folders named `redacted` (any case, any depth) and the Synology system folders
  `@eaDir`, `#recycle` and `#snapshot`.
- Writes the redacted copy **before** the original. If a run stops halfway, the original is
  missing and the next run redoes both.
- Writes `reports/<date>_<time>.csv` with one row per file it touched:
  `redacted`, `no-pii`, `not-redactable` (with the reason), `failed`, or `dry-run`.
  The script exits with code 1 if anything failed.

## Usage

```bash
./sync.sh --dry-run          # show what would upload, change nothing
./sync.sh                    # every folder in config.toml
./sync.sh MPAL YARENA        # only these folders
```

The first run creates `.venv` and installs the dependencies.

## One-time setup

### 1. Mount the NAS shares (read-only)

```bash
sudo apt install -y cifs-utils smbclient
smbclient -L //192.168.0.50 -U <nas-user>          # list the share names

sudo install -m 600 /dev/null /etc/nas-credentials
sudoedit /etc/nas-credentials                       # username=...  password=...

sudo mkdir -p /mnt/nas/MPAL
echo '//192.168.0.50/MPAL /mnt/nas/MPAL cifs ro,credentials=/etc/nas-credentials,uid=1000,gid=1000,iocharset=utf8,_netdev,nofail 0 0' \
  | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount -a
```

Repeat the `mkdir` and `fstab` lines for each share. `ro` means the script can't change anything
on the NAS.

### 2. AWS credentials (upload-only IAM user)

The `nas-s3-sync` IAM user may list the bucket and upload to it, but **cannot delete** anything
(`iam/nas-s3-sync-policy.json`).

```bash
aws iam create-user --user-name nas-s3-sync
aws iam put-user-policy --user-name nas-s3-sync --policy-name nas-s3-sync \
  --policy-document file://iam/nas-s3-sync-policy.json
aws iam create-access-key --user-name nas-s3-sync     # then:
aws configure --profile nas-s3-sync                   # paste the key, region ap-southeast-1
```

### 3. Config

```bash
cp config.example.toml config.toml   # list the shares under [folders]
```

`config.toml` is git-ignored. Adding a person means adding one line under `[folders]` and one
mount.

## Bucket

`envicomm-nas-datahub` (ap-southeast-1) has versioning on, public access blocked and AES256
encryption. It replaces `envicomm-nas-backup`. Nothing triggers on it: redaction happens only in
this script.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Known limits

- A file edited without its size changing is not uploaded again.
- When a changed file no longer contains PII, its old redacted copy stays (the IAM user can't
  delete). Remove it by hand if needed.
- Scanned PDFs have no text layer, so nothing in them is found (no OCR).
