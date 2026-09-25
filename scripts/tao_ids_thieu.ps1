# Sinh danh sách trip_hotel_id còn thiếu, để dùng với --ids-file.
#   powershell -ExecutionPolicy Bypass -File scripts\tao_ids_thieu.ps1
#
# CHỈ XÉT 3 PHẦN FILE DUMP CẦN: description · policy · surrounding (lân cận).
# Phòng / giá / ảnh / tiện nghi KHÔNG xét. Bỏ qua TP. Hồ Chí Minh.
# Chính sách bỏ section_code='credit' (anh mentor không lấy phương thức thanh toán).

param([string]$Container)
$ErrorActionPreference = "Stop"
$goc   = Split-Path -Parent $PSScriptRoot
$raDir = Join-Path $goc "output\ids"
New-Item -ItemType Directory -Force -Path $raDir | Out-Null

$env_ = @{}
Get-Content (Join-Path $goc ".env") | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $env_[$Matches[1]] = $Matches[2].Trim('"') }
}
if (-not $Container) {
    foreach ($ten in (docker ps --format "{{.Names}}")) {
        docker exec $ten psql --version *> $null
        if ($LASTEXITCODE -eq 0) { $Container = $ten; break }
    }
}
if (-not $Container) { throw "Khong tim thay container co psql." }
Write-Host "Container: $Container"

# --- Các mảnh điều kiện dùng chung -------------------------------------------
$bo_hcm = "AND NOT EXISTS (SELECT 1 FROM v2.city_i18n cx WHERE cx.city_id = h.city_id AND cx.name ILIKE '%Ch_ Minh%')"

function CoMoTa($lang)  { "EXISTS (SELECT 1 FROM v2.hotel_i18n i WHERE i.hotel_id=h.id AND i.locale LIKE '$lang%' AND COALESCE(btrim(i.description),'') <> '')" }
function CoPolicy($lang){ "EXISTS (SELECT 1 FROM v2.hotel_policy_sections p WHERE p.hotel_id=h.id AND p.locale LIKE '$lang%' AND p.section_code <> 'credit')" }
function CoLanCan($lang){ "EXISTS (SELECT 1 FROM v2.hotel_nearby_places np JOIN v2.place_i18n pi ON pi.place_id=np.place_id AND pi.locale LIKE '$lang%' AND COALESCE(btrim(pi.name),'') <> '' WHERE np.hotel_id=h.id)" }

function Truy($dieuKien) { "SELECT h.trip_hotel_id FROM v2.hotels h WHERE ($dieuKien) $bo_hcm ORDER BY h.trip_hotel_id" }

$bo = @(
  # --- 2 file chính: dùng cái này để crawl ---
  @{ ten = "thieu_vi"; mo_ta = "VI: thieu mo ta HOAC policy HOAC lan can  << crawl --only vi";
     sql = Truy("NOT $(CoMoTa 'vi') OR NOT $(CoPolicy 'vi') OR NOT $(CoLanCan 'vi')") },
  @{ ten = "thieu_en"; mo_ta = "EN: thieu mo ta HOAC policy HOAC lan can  << crawl --only en";
     sql = Truy("NOT $(CoMoTa 'en') OR NOT $(CoPolicy 'en') OR NOT $(CoLanCan 'en')") },
  # --- 6 file chi tiet: chi de soi xem thieu phan nao ---
  @{ ten = "thieu_mota_vi";   mo_ta = "chi tiet: thieu description VI";  sql = Truy("NOT $(CoMoTa 'vi')") },
  @{ ten = "thieu_policy_vi"; mo_ta = "chi tiet: thieu policy VI";       sql = Truy("NOT $(CoPolicy 'vi')") },
  @{ ten = "thieu_lancan_vi"; mo_ta = "chi tiet: thieu surrounding VI";  sql = Truy("NOT $(CoLanCan 'vi')") },
  @{ ten = "thieu_mota_en";   mo_ta = "chi tiet: thieu description EN";  sql = Truy("NOT $(CoMoTa 'en')") },
  @{ ten = "thieu_policy_en"; mo_ta = "chi tiet: thieu policy EN";       sql = Truy("NOT $(CoPolicy 'en')") },
  @{ ten = "thieu_lancan_en"; mo_ta = "chi tiet: thieu surrounding EN";  sql = Truy("NOT $(CoLanCan 'en')") }
)

foreach ($b in $bo) {
    $trong = "/tmp/$($b.ten).txt"
    docker exec -e PGPASSWORD=$($env_["DB_PASSWORD"]) $Container `
        psql -U $($env_["DB_USER"]) -d $($env_["DB_NAME"]) -v ON_ERROR_STOP=1 -At -c $b.sql -o $trong | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "psql loi o buoc $($b.ten)" }
    $ngoai = Join-Path $raDir "$($b.ten).txt"
    docker cp "${Container}:$trong" $ngoai | Out-Null
    $n = (Get-Content $ngoai | Where-Object { $_ -ne "" }).Count
    Write-Host ("{0,-18} {1,6} id   {2}" -f $b.ten, $n, $b.mo_ta)
}
Write-Host ""
Write-Host "Cac file nam o: $raDir"
Write-Host "Crawl bu:"
Write-Host "  python scripts\crawl_v2.py --ids-file output\ids\thieu_vi.txt --only vi --fast --chi-dump --redo --workers 3"
Write-Host "  python scripts\crawl_v2.py --ids-file output\ids\thieu_en.txt --only en --fast --chi-dump --redo --workers 3"
