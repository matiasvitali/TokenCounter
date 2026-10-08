# Ejecutar como Administrador. Quita la tarea programada y el recurso compartido.
# Por defecto conserva data\ (tokens.db) y el reporte; con -Purge también los borra y restaura los permisos de esta carpeta.
# Uso: .\uninstall.ps1 [-Purge] [-ReportDir C:\TokenReports] [-ShareName TokenReports]
param(
  [switch]$Purge,
  [string]$ReportDir = "C:\TokenReports",
  [string]$ShareName = "TokenReports"
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot   # scripts\ -> carpeta del programa

Stop-ScheduledTask -TaskName "TokenCounter" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "TokenCounter" -Confirm:$false -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "TokenCounterOtel" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "TokenCounterOtel" -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like "*otel_receiver.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($py) { & $py -I "$root\src\managed_settings.py" remove }  # quita solo las claves env que puso el instalador
if (Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue) { Remove-SmbShare -Name $ShareName -Force }

if ($Purge) {
  Remove-Item $ReportDir -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item "$root\data" -Recurse -Force -ErrorAction SilentlyContinue
  icacls $root /reset /T | Out-Null   # vuelve a heredar permisos para poder borrar la carpeta
  Write-Host "Desinstalado y datos borrados. Ya puedes eliminar la carpeta $root."
} else {
  Write-Host "Desinstalado. Se conservaron tokens.db y $ReportDir (usa -Purge para borrarlos)."
}
