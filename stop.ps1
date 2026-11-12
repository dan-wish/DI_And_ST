Write-Host "[*] Stopping F1 Pit Wall service on port 5000..." -ForegroundColor Cyan
$connections = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
if ($connections) {
    $pids = $connections | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($p in $pids) {
        Write-Host "[*] Stopping Process ID: $p" -ForegroundColor Yellow
        try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {}
    }
    Write-Host "[OK] Port 5000 is now free." -ForegroundColor Green
} else {
    Write-Host "[*] No active service listening on port 5000." -ForegroundColor Green
}
