# Locate the newest real Node.js installation (fnm store or system install).
# Prints the directory containing node.exe, or nothing if not found.
$candidates = @(
  Get-ChildItem "$env:APPDATA\fnm\node-versions\*\installation\node.exe" -ErrorAction SilentlyContinue
  Get-ChildItem "$env:LOCALAPPDATA\fnm\node-versions\*\installation\node.exe" -ErrorAction SilentlyContinue
  Get-Item "$env:ProgramFiles\nodejs\node.exe" -ErrorAction SilentlyContinue
) | Where-Object { $_ }

$best = $candidates | Sort-Object { [version]$_.VersionInfo.ProductVersion } | Select-Object -Last 1
if ($best) { Write-Output $best.DirectoryName }
