<#
NCTR-200 현장 시험용 Windows 보조 스크립트 (Windows 핫스팟 구성)

작업 디렉터리를 research로 두고 실행합니다. Dns* 동작은 관리자 PowerShell이 필요합니다.

  .\tools\nctr_windows_field.ps1 -Action DnsShow
  .\tools\nctr_windows_field.ps1 -Action DnsSet -InterfaceAlias 'Ethernet' -UbuntuDns 192.168.1.10
  .\tools\nctr_windows_field.ps1 -Action DnsCheck -UbuntuDns 192.168.1.10
  .\tools\nctr_windows_field.ps1 -Action DnsRestore -InterfaceAlias 'Ethernet'
  .\tools\nctr_windows_field.ps1 -Action Retrieve -SshTarget USER@UBUNTU_HOST -Date 20261120
  .\tools\nctr_windows_field.ps1 -Action AttachWireshark -RunId field-20261120-00 -Path .\captures\hotspot_20261120.pcapng

DnsSet은 처음 실행할 때만 원래 DNS를 백업합니다. 백업은 %LOCALAPPDATA%\nctr-lab에 저장됩니다.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('DnsShow', 'DnsSet', 'DnsCheck', 'DnsRestore', 'Retrieve', 'AttachWireshark')]
    [string]$Action,
    [string]$InterfaceAlias,
    [string]$UbuntuDns,
    [string]$SshTarget,
    [string]$Date,
    [string]$RunId,
    [string]$Path
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$backupDir = Join-Path $env:LOCALAPPDATA 'nctr-lab'

function Assert-Admin {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '관리자 PowerShell에서 실행하십시오.'
    }
}

function Assert-Value([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "-$Name 값이 필요합니다." }
}

function Get-PythonExe {
    $preferred = Join-Path $env:LOCALAPPDATA 'Python\bin\python.exe'
    if (Test-Path -LiteralPath $preferred) { return $preferred }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike '*WindowsApps*') { return $cmd.Source }
    throw 'python.exe를 찾지 못했습니다.'
}

function Get-BackupPath([string]$Alias) {
    $guid = (Get-NetAdapter -InterfaceAlias $Alias).InterfaceGuid.Trim('{}')
    return (Join-Path $backupDir "dns_backup_$guid.json")
}

function Get-StaticDns([string]$Alias) {
    $adapter = Get-NetAdapter -InterfaceAlias $Alias
    $key = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$($adapter.InterfaceGuid)"
    $value = (Get-ItemProperty -LiteralPath $key -Name NameServer -ErrorAction SilentlyContinue).NameServer
    if ([string]::IsNullOrWhiteSpace($value)) { return @() }
    return @($value -split '[,\s]+' | Where-Object { $_ })
}

function Show-Dns {
    Get-DnsClientServerAddress |
        Where-Object { $_.ServerAddresses.Count -gt 0 } |
        Sort-Object InterfaceAlias, AddressFamily |
        Format-Table InterfaceAlias, @{n = 'Family'; e = { if ($_.AddressFamily -eq 2) { 'IPv4' } else { 'IPv6' } } }, ServerAddresses -AutoSize
}

function Test-Names([string]$Server) {
    Clear-DnsClientCache
    foreach ($name in 'igs4.krb.co.kr', 'igis.krb.co.kr') {
        try {
            $ips = (Resolve-DnsName -Name $name -Type A -DnsOnly | Where-Object { $_.Type -eq 'A' }).IPAddress -join ', '
        } catch { $ips = "조회 실패: $($_.Exception.Message)" }
        Write-Host ("Windows 기본 경로  {0,-16} -> {1}" -f $name, $ips)
        if ($Server) {
            try {
                $ips = (Resolve-DnsName -Name $name -Type A -DnsOnly -Server $Server | Where-Object { $_.Type -eq 'A' }).IPAddress -join ', '
            } catch { $ips = "조회 실패: $($_.Exception.Message)" }
            Write-Host ("{0,-17} {1,-16} -> {2}" -f "@$Server", $name, $ips)
        }
    }
    Write-Host '판정: igs4는 Ubuntu IP, igis는 제조사 IP여야 합니다. 조절기가 실제로 받은 응답은 Wireshark에서 확인합니다.'
}

switch ($Action) {
    'DnsShow' {
        Show-Dns
        Get-NetAdapter | Where-Object Status -eq 'Up' | Format-Table Name, InterfaceDescription, Status -AutoSize
    }

    'DnsSet' {
        Assert-Admin
        Assert-Value 'InterfaceAlias' $InterfaceAlias
        Assert-Value 'UbuntuDns' $UbuntuDns
        [void][System.Net.IPAddress]::Parse($UbuntuDns)
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        $backup = Get-BackupPath $InterfaceAlias
        if (Test-Path -LiteralPath $backup) {
            Write-Host "백업이 이미 있어 유지합니다: $backup"
        } else {
            $record = [ordered]@{
                interface   = $InterfaceAlias
                saved_at    = (Get-Date).ToString('o')
                static_ipv4 = @(Get-StaticDns $InterfaceAlias)
                effective   = @((Get-DnsClientServerAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4).ServerAddresses)
            }
            $record | ConvertTo-Json | Set-Content -LiteralPath $backup -Encoding utf8
            Write-Host "원래 DNS를 백업했습니다: $backup"
            Write-Host ("  고정 DNS: {0}" -f ($(if ($record.static_ipv4.Count) { $record.static_ipv4 -join ', ' } else { '없음(DHCP)' })))
        }
        Set-DnsClientServerAddress -InterfaceAlias $InterfaceAlias -ServerAddresses $UbuntuDns
        Clear-DnsClientCache
        $v6 = (Get-DnsClientServerAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv6).ServerAddresses
        if ($v6.Count -gt 0) {
            Write-Warning ("이 어댑터에 IPv6 DNS가 있습니다: {0}. 아래 결과에서 igs4가 Ubuntu IP가 아니면 IPv6 DNS가 응답하는 것일 수 있습니다." -f ($v6 -join ', '))
        }
        Test-Names $UbuntuDns
        Write-Host '다음: 설정 → 모바일 핫스팟을 껐다 켭니다.'
    }

    'DnsCheck' {
        Test-Names $UbuntuDns
    }

    'DnsRestore' {
        Assert-Admin
        Assert-Value 'InterfaceAlias' $InterfaceAlias
        $backup = Get-BackupPath $InterfaceAlias
        if (-not (Test-Path -LiteralPath $backup)) { throw "백업이 없습니다: $backup" }
        $record = Get-Content -LiteralPath $backup -Raw | ConvertFrom-Json
        $static = @($record.static_ipv4 | Where-Object { $_ })
        if ($static.Count -gt 0) {
            Set-DnsClientServerAddress -InterfaceAlias $InterfaceAlias -ServerAddresses $static
            Write-Host ("고정 DNS로 복구했습니다: {0}" -f ($static -join ', '))
        } else {
            Set-DnsClientServerAddress -InterfaceAlias $InterfaceAlias -ResetServerAddresses
            Write-Host 'DHCP 자동 DNS로 복구했습니다.'
        }
        Clear-DnsClientCache
        $done = $backup -replace '\.json$', ('.restored_{0}.json' -f (Get-Date -Format 'yyyyMMddHHmmss'))
        Move-Item -LiteralPath $backup -Destination $done
        Test-Names $null
        Write-Host '다음: 모바일 핫스팟을 껐다 켭니다.'
    }

    'Retrieve' {
        Assert-Value 'SshTarget' $SshTarget
        Assert-Value 'Date' $Date
        if ($Date -notmatch '^\d{8}$') { throw '-Date는 YYYYMMDD 형식입니다.' }
        Set-Location -LiteralPath $repoRoot
        $python = Get-PythonExe
        New-Item -ItemType Directory -Path .\experiments -Force | Out-Null
        $runs = @(ssh $SshTarget "ls -1 nctr-lab/experiments 2>/dev/null | grep -E '^field-$Date-[A-Za-z0-9-]+$'")
        if ($LASTEXITCODE -ne 0 -or $runs.Count -eq 0) { throw "원격에 field-$Date-* 회차가 없습니다." }
        foreach ($run in $runs) {
            $run = $run.Trim()
            if (Test-Path -LiteralPath ".\experiments\$run") {
                Write-Warning "이미 있어 건너뜁니다: experiments\$run"
                continue
            }
            Write-Host "== 회수: $run"
            scp -r "${SshTarget}:nctr-lab/experiments/$run" .\experiments\
            if ($LASTEXITCODE -ne 0) { throw "scp 실패: $run" }
            & $python src/nctr_lab_cli.py verify --run-dir "experiments/$run"
            Write-Host "verify 종료 코드: $LASTEXITCODE"
        }
    }

    'AttachWireshark' {
        Assert-Value 'RunId' $RunId
        Assert-Value 'Path' $Path
        $source = (Resolve-Path -LiteralPath $Path).Path
        Set-Location -LiteralPath $repoRoot
        $python = Get-PythonExe
        & $python src/nctr_lab_cli.py attach --run-dir "experiments/$RunId" --input $source --category network
        & $python src/nctr_lab_cli.py verify --run-dir "experiments/$RunId"
        Write-Host "verify 종료 코드: $LASTEXITCODE"
    }
}
