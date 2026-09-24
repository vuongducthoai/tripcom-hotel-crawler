# Sinh lai output\dump.sql va output\dump.txt tu database v2.
#
#   powershell -ExecutionPolicy Bypass -File scripts\xuat_dump.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\xuat_dump.ps1 -Container tripcom-postgres
#
# Chay psql BEN TRONG container va ghi file bang -o, roi moi docker cp ra ngoai.
# Khong dung pipeline cua PowerShell vi console se doi bang ma va lam hong
# tieng Viet (kieu "Tß╗ìa lß║íc").

param([string]$Container)

$ErrorActionPreference = "Stop"
$goc = Split-Path -Parent $PSScriptRoot
$script = Join-Path $goc "scripts\export_trip_property_translation.sql"
$raDir  = Join-Path $goc "output"
New-Item -ItemType Directory -Force -Path $raDir | Out-Null

# Doc thong so ket noi tu .env
$env_ = @{}
Get-Content (Join-Path $goc ".env") | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $env_[$Matches[1]] = $Matches[2].Trim('"') }
}
$dbUser = $env_["DB_USER"]; $dbName = $env_["DB_NAME"]; $dbPass = $env_["DB_PASSWORD"]

# Tim container CO SAN psql. Khong doan theo ten vi pgadmin cung khop "pg"
# nhung ben trong khong co psql.
if (-not $Container) {
    $ungVien = @()
    docker ps --format "{{.Names}}`t{{.Image}}" | ForEach-Object {
        $ten, $anh = $_ -split "`t", 2
        if ($anh -match "postgres|timescale|postgis" -and $anh -notmatch "pgadmin") { $ungVien += $ten }
    }
    # Con thieu thi thu het moi container dang chay
    if (-not $ungVien) { $ungVien = docker ps --format "{{.Names}}" }
    foreach ($ten in $ungVien) {
        docker exec $ten psql --version *> $null
        if ($LASTEXITCODE -eq 0) { $Container = $ten; break }
    }
}
if (-not $Container) {
    Write-Host "Cac container dang chay:"
    docker ps --format "  {{.Names}}  ({{.Image}})"
    throw "Khong container nao co psql. Chay lai voi: .\scripts\xuat_dump.ps1 -Container <ten>"
}
$ct = $Container
Write-Host "Container: $ct"

docker cp $script "${ct}:/tmp/exp.sql" | Out-Null

# dump.sql  -> co tien to splatform_meta.
docker exec -e PGPASSWORD=$dbPass $ct psql -U $dbUser -d $dbName -v ON_ERROR_STOP=1 -At -f /tmp/exp.sql -o /tmp/dump.sql
if ($LASTEXITCODE -ne 0) { throw "psql loi khi sinh dump.sql" }
# dump.txt  -> khong co tien to schema
docker exec -e PGPASSWORD=$dbPass $ct psql -U $dbUser -d $dbName -v ON_ERROR_STOP=1 -At -v pfx= -f /tmp/exp.sql -o /tmp/dump.txt
if ($LASTEXITCODE -ne 0) { throw "psql loi khi sinh dump.txt" }

docker cp "${ct}:/tmp/dump.sql" (Join-Path $raDir "dump.sql") | Out-Null
docker cp "${ct}:/tmp/dump.txt" (Join-Path $raDir "dump.txt") | Out-Null

$n1 = (Get-Content (Join-Path $raDir "dump.sql")).Count
$n2 = (Get-Content (Join-Path $raDir "dump.txt")).Count
Write-Host "output\dump.sql : $n1 dong"
Write-Host "output\dump.txt : $n2 dong"

# Thong ke nhanh: bao nhieu row_uuid co du ca 2 ngon ngu
docker exec -e PGPASSWORD=$dbPass $ct psql -U $dbUser -d $dbName -c @"
SELECT i.locale, count(DISTINCT h.id) AS so_khach_san
FROM v2.hotels h JOIN v2.hotel_i18n i ON i.hotel_id = h.id
WHERE i.description IS NOT NULL GROUP BY i.locale ORDER BY 1;
"@
