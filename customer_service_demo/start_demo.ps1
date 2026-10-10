$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$localRoot = Join-Path $projectRoot '.local'
$appUrl = 'http://127.0.0.1:8787'
New-Item -ItemType Directory -Path $localRoot -Force | Out-Null
try {
    $health = Invoke-RestMethod -Uri "$appUrl/api/bootstrap" -TimeoutSec 2
    if ($health.application -ne 'snack-customer-service') { throw '端口 8787 已被其他程序使用。' }
    Start-Process $appUrl
    exit 0
} catch {
    if ($_.Exception.Message -like '*端口 8787*') { throw }
}
$pythonPath = (Get-Command python).Source
$serviceProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'runtime.web', '--port', '8787') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $localRoot 'server.stdout.log') -RedirectStandardError (Join-Path $localRoot 'server.stderr.log')
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 350
    if ($serviceProcess.HasExited) { throw '客服服务没有启动成功，请查看 .local/server.stderr.log。' }
    try {
        $health = Invoke-RestMethod -Uri "$appUrl/api/bootstrap" -TimeoutSec 1
        if ($health.application -eq 'snack-customer-service') {
            $serviceProcess.Id | Set-Content -Path (Join-Path $localRoot 'server.pid')
            Start-Process $appUrl
            exit 0
        }
    } catch {}
}
throw '客服服务启动时间较长，请稍后打开 http://127.0.0.1:8787。'
