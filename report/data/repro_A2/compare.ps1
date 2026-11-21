# 三方对照：原始 baseline / 本轮 A 跑 / 本轮 A2 跑
$refPath = "D:\phpstudy_pro\WWW\_bench_seed\result_A.csv"
$aPath   = "D:\phpstudy_pro\WWW\snipe-it\data\result_A.csv"
$a2Path  = "D:\phpstudy_pro\WWW\snipe-it\data\result_A2.csv"

$nameMap = @{ "E1_login" = "E1"; "E2_dashboard" = "E2"; "E3_assetlist" = "E3"; "E4_assetdetail" = "E4" }

function Get-Median3 {
    param([double[]]$vals)
    if ($vals.Count -eq 0) { return [double]::NaN }
    $s = $vals | Sort-Object
    if ($s.Count % 2 -eq 1) { return [double]$s[[int](($s.Count - 1) / 2)] }
    return [double](($s[$s.Count / 2 - 1] + $s[$s.Count / 2]) / 2)
}

function Load-Stats {
    param([string]$path)
    $rows = Import-Csv $path
    $g = @{}
    foreach ($row in $rows) {
        $ep = $row.endpoint
        if ($nameMap.ContainsKey($ep)) { $ep = $nameMap[$ep] }
        $key = "$ep|$($row.tier)"
        if (-not $g.ContainsKey($key)) { $g[$key] = @{ rps=@(); mean=@(); p95=@(); err=@(); qpr=@() } }
        $x = $g[$key]
        $x.rps  += [double]$row.rps
        $x.mean += [double]$row.tpr_mean
        $x.p95  += [double]$row.p95
        $err = if ([string]::IsNullOrWhiteSpace($row.non2xx)) { 0 } else { [double]$row.non2xx }
        $x.err += [double](100 * $err / [double]$row.complete)
        if (-not [string]::IsNullOrWhiteSpace($row.q_used)) { $x.qpr += [double]([double]$row.q_used / [double]$row.n) }
    }
    $o = @{}
    foreach ($k in $g.Keys) {
        $o[$k] = [pscustomobject]@{
            rps  = Get-Median3 $g[$k].rps
            mean = Get-Median3 $g[$k].mean
            p95  = Get-Median3 $g[$k].p95
            err  = Get-Median3 $g[$k].err
            qpr  = Get-Median3 $g[$k].qpr
        }
    }
    return $o
}

$R = Load-Stats $refPath
$A = Load-Stats $aPath
$A2 = Load-Stats $a2Path

function Pct { param($new, $old) if ($old -eq 0) { return 0 } return [Math]::Round(100 * ($new - $old) / $old, 1) }

$rows = @()
foreach ($ep in @("E1","E2","E3","E4")) {
    foreach ($c in @(1,5,10,20,30)) {
        $k = "$ep|$c"
        if (-not $R.ContainsKey($k)) { continue }
        $rows += [pscustomobject]@{
            ep        = $ep
            c         = $c
            rps_ref   = [Math]::Round($R[$k].rps, 2)
            rps_A     = [Math]::Round($A[$k].rps, 2)
            d_rps_A   = Pct $A[$k].rps $R[$k].rps
            rps_A2    = [Math]::Round($A2[$k].rps, 2)
            d_rps_A2  = Pct $A2[$k].rps $R[$k].rps
            mean_ref  = [Math]::Round($R[$k].mean, 0)
            mean_A2   = [Math]::Round($A2[$k].mean, 0)
            d_mean_A2 = Pct $A2[$k].mean $R[$k].mean
            p95_ref   = [Math]::Round($R[$k].p95, 0)
            p95_A2    = [Math]::Round($A2[$k].p95, 0)
            d_p95_A2  = Pct $A2[$k].p95 $R[$k].p95
            err_ref   = [Math]::Round($R[$k].err, 1)
            err_A2    = [Math]::Round($A2[$k].err, 1)
            qpr_ref   = [Math]::Round($R[$k].qpr, 0)
            qpr_A2    = [Math]::Round($A2[$k].qpr, 0)
        }
    }
}
$rows | Format-Table -AutoSize

$dRps  = $rows | ForEach-Object { [Math]::Abs($_.d_rps_A2) }
$dMean = $rows | ForEach-Object { [Math]::Abs($_.d_mean_A2) }
$dP95  = $rows | ForEach-Object { [Math]::Abs($_.d_p95_A2) }
$dRpsA = $rows | ForEach-Object { [Math]::Abs($_.d_rps_A) }
""
"=== A2 vs 原始：平均绝对偏差 ==="
"rps  : avg=" + [Math]::Round(($dRps | Measure-Object -Average).Average,1) + "%  max=" + [Math]::Round(($dRps | Measure-Object -Maximum).Maximum,1) + "%"
"mean : avg=" + [Math]::Round(($dMean | Measure-Object -Average).Average,1) + "%  max=" + [Math]::Round(($dMean | Measure-Object -Maximum).Maximum,1) + "%"
"p95  : avg=" + [Math]::Round(($dP95 | Measure-Object -Average).Average,1) + "%  max=" + [Math]::Round(($dP95 | Measure-Object -Maximum).Maximum,1) + "%"
"=== A 跑 vs 原始（对照）==="
"rps  : avg=" + [Math]::Round(($dRpsA | Measure-Object -Average).Average,1) + "%  max=" + [Math]::Round(($dRpsA | Measure-Object -Maximum).Maximum,1) + "%"
