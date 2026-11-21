#Requires -Version 5.1
<#
  run_bench.ps1 — ApacheBench 压测批跑脚本（v2）
  只跑 ab 和 mysql 的只读查询，不改应用代码、不改 nginx/php 配置、不碰 php-cgi 进程。

  v2 相对 v1 的修正：
   1. total_time 正则：ab 2.3 实际输出为 "Time taken for tests:"，v1 写成了 "Total time taken for tests:"，永远匹配不上。
   2. -Session 为空时 throw 并退出，不再"警告后继续跑"（否则 E2-E4 全 302 到登录页，写进假数字）。
   3. 每批独立产物：result_<BatchName>.csv + raw_<BatchName>\，开跑前清空同名旧产物，避免两批数据混在一个 csv。
   4. MySQL 密码不再硬编码，改为 -DbPass 或 $env:MYSQL_PWD，缺失时 throw。
   5. 检查 ab 的 $LASTEXITCODE，失败轮标记 FAILED 并计数，最后汇总。
#>

param(
    [string]$Target     = "http://127.0.0.1",
    [string]$HostHeader = "snipe.local",
    [string]$Session    = "",
    [string]$OutDir     = ".\data",
    [string]$BatchName  = "A",
    [string]$DbUser     = "root",
    [string]$DbPass     = "",
    [string]$OnlyEndpoint = "",
    [int]$OnlyTier      = 0,
    [int]$OnlyRep       = 0
)

$ErrorActionPreference = "Stop"

# ---------- 工具路径 ----------
$ab    = "D:\phpstudy_pro\Extensions\Apache2.4.39\bin\ab.exe"
$mysql = "D:\phpstudy_pro\Extensions\MySQL5.7.26\bin\mysql.exe"

if (-not (Test-Path $ab))    { throw "ab.exe not found: $ab" }
if (-not (Test-Path $mysql)) { throw "mysql.exe not found: $mysql" }

# ---------- 前置校验（不满足就直接停，不留半套数据）----------
if ([string]::IsNullOrWhiteSpace($Session)) {
    Write-Error "缺少 -Session：E2/E3/E4 需要已登录的 snipeit_session，否则全部 302 到 /login，测出来的是 9 KB 登录页。"
    exit 1
}

$pwd = if ($DbPass -ne "") { $DbPass } else { $env:MYSQL_PWD }
if ([string]::IsNullOrWhiteSpace($pwd)) {
    throw "缺少数据库密码：传 -DbPass，或先 `$env:MYSQL_PWD='...' 再跑。"
}

# ---------- 目录与产物（每批独立，开跑前清空）----------
$rawDir = Join-Path $OutDir ("raw_" + $BatchName)
$csvPath = Join-Path $OutDir ("result_" + $BatchName + ".csv")

foreach ($p in @($rawDir, $csvPath)) {
    if (Test-Path $p) {
        Write-Host "[CLEAN] 删除旧产物 $p"
        Remove-Item $p -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $rawDir | Out-Null
$header = "endpoint,tier,rep,n,complete,failed,non2xx,rps,tpr_mean,tpr_conc,total_time,p50,p90,p95,p99,q_used"
Set-Content -Path $csvPath -Value $header -Encoding ASCII

# ---------- 端点定义 ----------
$endpoints = @(
    @{ tag = "E1"; path = "/login";           needSession = $false },
    @{ tag = "E2"; path = "/";                needSession = $true  },
    @{ tag = "E3"; path = "/hardware?page=1"; needSession = $true  },
    @{ tag = "E4"; path = "/hardware/1";      needSession = $true  }
)

$concurrencyTiers = @(1, 5, 10, 20, 30)

# ---------- MySQL Questions ----------
function Get-MySqlQuestions {
    param([string]$mysqlExe, [string]$user, [string]$pass)
    # 1) & $mysql 整体调用先收进变量再解析，避免 PowerShell 把 -uroot 当减法。
    # 2) mysql 的 "Using a password" 走 stderr，在 Stop 策略下会被当成 NativeError，故临时降级。
    # 3) 密码走环境变量 MYSQL_PWD，不放在命令行上（避免出现在进程列表里）。
    $prevErrAct = $ErrorActionPreference
    $prevPwd    = $env:MYSQL_PWD
    $ErrorActionPreference = "SilentlyContinue"
    $env:MYSQL_PWD = $pass
    try {
        $output = & $mysqlExe "-u$user" -N -B -e "SHOW GLOBAL STATUS LIKE 'Questions';" 2>&1
    } finally {
        $env:MYSQL_PWD = $prevPwd
        $ErrorActionPreference = $prevErrAct
    }
    foreach ($line in ($output -as [array])) {
        if ("$line" -match '^Questions\s+(\d+)$') { return [int64]$Matches[1] }
    }
    return $null
}

# ---------- 解析 ab 输出 ----------
function Parse-AbOutput {
    param([string]$text)

    $r = [pscustomobject]@{
        complete = ""; failed = ""; non2xx = ""; rps = ""
        tpr_mean = ""; tpr_conc = ""; total_time = ""
        p50 = ""; p90 = ""; p95 = ""; p99 = ""
    }

    foreach ($ln in ($text -split "`r?`n")) {
        if ($ln -match 'Complete requests:\s*(\d+)')              { $r.complete   = $Matches[1] }
        if ($ln -match 'Failed requests:\s*(\d+)')                { $r.failed     = $Matches[1] }
        if ($ln -match 'Non-2xx responses:\s*(\d+)')              { $r.non2xx     = $Matches[1] }
        if ($ln -match 'Requests per second:\s*([0-9.]+)')        { $r.rps        = $Matches[1] }
        # ab 2.3 的实际措辞是 "Time taken for tests:   6.169 seconds"
        if ($ln -match 'Time taken for tests:\s*([0-9.]+)')       { $r.total_time = $Matches[1] }

        # Time per request 两行：第一行 (mean)，第二行 (mean, across all concurrent requests)
        if ($ln -match 'Time per request:\s*([0-9.]+)\s*\[ms\]\s*\(mean\)') {
            if ($r.tpr_mean -eq "") { $r.tpr_mean = $Matches[1] }
        }
        if ($ln -match 'Time per request:\s*([0-9.]+)\s*\[ms\]\s*\(mean,\s*across all concurrent requests\)') {
            $r.tpr_conc = $Matches[1]
        }

        if ($ln -match '^\s*50%\s+([0-9.]+)') { $r.p50 = $Matches[1] }
        if ($ln -match '^\s*90%\s+([0-9.]+)') { $r.p90 = $Matches[1] }
        if ($ln -match '^\s*95%\s+([0-9.]+)') { $r.p95 = $Matches[1] }
        if ($ln -match '^\s*99%\s+([0-9.]+)') { $r.p99 = $Matches[1] }
    }
    return $r
}

# ---------- 主循环 ----------
$totalRuns = $endpoints.Count * $concurrencyTiers.Count * 3
$runIndex = 0; $okCount = 0; $failCount = 0
$sw = [System.Diagnostics.Stopwatch]::StartNew()

foreach ($ep in $endpoints) {
    if ($OnlyEndpoint -ne "" -and $ep.tag -ne $OnlyEndpoint) { continue }
    $url = $Target + $ep.path

    foreach ($c in $concurrencyTiers) {
        if ($OnlyTier -gt 0 -and $c -ne $OnlyTier) { continue }
        $n = [Math]::Max(20, $c * 8)

        for ($rep = 1; $rep -le 3; $rep++) {
            if ($OnlyRep -gt 0 -and $rep -ne $OnlyRep) { continue }
            $runIndex++
            Write-Host ("[{0}/{1}] {2} c={3} n={4} rep={5}  {6}" -f $runIndex,$totalRuns,$ep.tag,$c,$n,$rep,$url)

            $qBefore = Get-MySqlQuestions -mysqlExe $mysql -user $DbUser -pass $pwd

            $rawFile = Join-Path $rawDir ("{0}_c{1}_r{2}.txt" -f $ep.tag,$c,$rep)
            $abArgs = @("-n","$n","-c","$c","-H","Host: $HostHeader")
            if ($ep.needSession) { $abArgs += @("-C","snipeit_session=$Session") }
            $abArgs += $url

            & $ab @abArgs *> $rawFile
            $exitCode = $LASTEXITCODE

            $qAfter = Get-MySqlQuestions -mysqlExe $mysql -user $DbUser -pass $pwd
            $qUsed  = ""
            if ($null -ne $qBefore -and $null -ne $qAfter) { $qUsed = ($qAfter - $qBefore).ToString() }

            if ($exitCode -ne 0) {
                $failCount++
                Write-Host ("  [FAIL] {0} c={1} r={2} exit={3}" -f $ep.tag,$c,$rep,$exitCode) -ForegroundColor Red
                $line = "$($ep.tag),$c,$rep,$n,FAILED,FAILED,FAILED,FAILED,FAILED,FAILED,FAILED,FAILED,FAILED,FAILED,FAILED,$qUsed"
            } else {
                $okCount++
                $p = Parse-AbOutput -text (Get-Content -Path $rawFile -Raw)
                $line = @($ep.tag,$c,$rep,$n,$p.complete,$p.failed,$p.non2xx,$p.rps,$p.tpr_mean,$p.tpr_conc,$p.total_time,$p.p50,$p.p90,$p.p95,$p.p99,$qUsed) -join ","
                Write-Host ("  rps={0} mean={1}ms p95={2}ms total={3}s q={4}" -f $p.rps,$p.tpr_mean,$p.p95,$p.total_time,$qUsed)
            }
            Add-Content -Path $csvPath -Value $line -Encoding ASCII

            if ($rep -lt 3) { Start-Sleep -Seconds 5 }
        }
        Start-Sleep -Seconds 2
    }
}

$sw.Stop()
$doneText = "DONE " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Set-Content -Path (Join-Path $OutDir ("matrix_done_" + $BatchName + ".txt")) -Value $doneText -Encoding ASCII

Write-Host ""
Write-Host ("{0}  batch={1}  ok={2} fail={3}  elapsed={4} min" -f $doneText,$BatchName,$okCount,$failCount,[Math]::Round($sw.Elapsed.TotalMinutes,1))
Write-Host "csv : $csvPath"
Write-Host "raw : $rawDir"
if ($failCount -gt 0) { exit 2 }
