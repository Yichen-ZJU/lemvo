param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-zA-Z0-9_.@:-]+$')][string]$SshHost,
    [string]$IdentityFile,
    [ValidateRange(1,65535)][int]$SshPort = 22,
    [ValidateRange(1,65535)][int]$LocalPort = 8765,
    [ValidateRange(1,65535)][int]$RemotePort = 8765
)
$ErrorActionPreference = 'Stop'
# Uses an already running console. Does not start/stop any research session.
$ConnectionArgs = @('-F','NUL','-p',"$SshPort",'-o','BatchMode=yes','-o','ConnectTimeout=10')
if ($IdentityFile) {
    $KeyPath = (Resolve-Path -LiteralPath $IdentityFile).Path
    # Start-Process joins arguments into a Windows command line.
    if ($KeyPath -match '["\r\n]') { throw 'Invalid identity file path.' }
    $ConnectionArgs += @('-i',$KeyPath)
}
$ConsoleUrl = & ssh @ConnectionArgs $SshHost '.local/bin/lemvo-console url'
if ($LASTEXITCODE -ne 0 -or $ConsoleUrl -notmatch '^http://127\.0\.0\.1:\d+/#token=([A-Za-z0-9_-]+)$') {
    throw 'Console URL unavailable. Start lemvo-console on the server first.'
}
$ConsoleToken = $Matches[1]
# Direct host address: omit inherited forwarding rules (they may collide with existing reverse tunnels).
$ForwardArgs = @('-F','NUL','-N','-p',"$SshPort",'-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3','-L',"${LocalPort}:127.0.0.1:${RemotePort}",$SshHost)
if ($IdentityFile) { $ForwardArgs = @('-i',('"'+$KeyPath+'"')) + $ForwardArgs }
$ForwardProcess = Start-Process -FilePath (Get-Command ssh.exe).Source -ArgumentList $ForwardArgs -WindowStyle Hidden -PassThru
Start-Sleep -Milliseconds 800
if ($ForwardProcess.HasExited) { throw 'SSH forward could not start. Choose a free -LocalPort and verify SSH connectivity.' }
Start-Process "http://127.0.0.1:${LocalPort}/#token=${ConsoleToken}"
Write-Output "Console opened. SSH tunnel PID: $($ForwardProcess.Id). Closing this tunnel leaves all experiments running."
