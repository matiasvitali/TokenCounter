# Ejecutar como Administrador en la VM. Registra una tarea SYSTEM que recolecta y regenera el reporte
# al arrancar y cada 10 min, y publica el reporte en una carpeta de solo lectura para los usuarios.
# Uso: .\install.ps1 [-Python python.exe] [-ReportDir C:\TokenReports] [-Share] [-ShareName TokenReports] [-NoOtel]
# -Share crea además el recurso de red \\<VM>\TokenReports (apagado por defecto).
# Salvo -NoOtel, también activa la telemetría OpenTelemetry de Claude Code (receptor local + managed-settings.json).
param(
  [string]$Python = (Get-Command python).Source,
  [string]$ReportDir = "C:\TokenReports",
  [switch]$Share,
  [string]$ShareName = "TokenReports",
  [switch]$NoOtel
)
$ErrorActionPreference = "Stop"
# SIDs conocidos (funcionan en cualquier idioma de Windows): SYSTEM, Administradores, Usuarios
$root = Split-Path $PSScriptRoot   # scripts\ -> carpeta del programa
$sys = "*S-1-5-18"; $adm = "*S-1-5-32-544"; $usr = "*S-1-5-32-545"

# 0. Seguridad: las tareas corren como SYSTEM, así que ni los scripts ni Python pueden ser modificables por usuarios.
#    Python: si un usuario puede escribir en su carpeta, puede ejecutar código como SYSTEM.
$pyDir = Split-Path $Python
$weak = "S-1-5-32-545", "S-1-5-11", "S-1-1-0", "S-1-5-4"   # Usuarios, Usuarios autenticados, Todos, INTERACTIVE
# WriteData/CreateFiles, AppendData, DeleteSubdirs, Delete, ChangePermissions, TakeOwnership, GENERIC_ALL, GENERIC_WRITE
$write = 0x2 -bor 0x4 -bor 0x40 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
$bad = (Get-Acl $pyDir).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object {
  $_.AccessControlType -eq "Allow" -and ([int64]$_.FileSystemRights -band $write) -and $weak -contains $_.IdentityReference.Value }
if ($Python -like "*WindowsApps*" -or $Python -like "$env:SystemDrive\Users\*" -or $bad) {
  throw "Python en '$pyDir' es modificable por usuarios o es por usuario. Instala Python para todos los usuarios (Program Files) y pasa -Python con esa ruta."
}
#    Carpeta de TokenCounter: solo SYSTEM y Administradores (scripts que corren como SYSTEM y tokens.db con datos de todos)
icacls $root /inheritance:r /grant:r "${sys}:(OI)(CI)F" "${adm}:(OI)(CI)F" /T /Q | Out-Null

# 1. Carpeta del reporte: escriben SYSTEM/Admins, leen todos los usuarios
New-Item -ItemType Directory -Force $ReportDir | Out-Null
icacls $ReportDir /inheritance:r /grant:r "${sys}:(OI)(CI)F" "${adm}:(OI)(CI)F" "${usr}:(OI)(CI)RX" | Out-Null

# 2. Recurso compartido de solo lectura (\\<VM>\TokenReports), solo con -Share
if ($Share -and -not (Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue)) {
  New-SmbShare -Name $ShareName -Path $ReportDir -ReadAccess "BUILTIN\Users" -FullAccess "BUILTIN\Administrators" | Out-Null
}

# 3. Tarea programada (python -I: modo aislado, ignora PYTHON* y el site-packages del usuario)
$out = Join-Path $ReportDir "report.html"
$cmd = "& '$Python' -I '$root\src\collect.py'; & '$Python' -I '$root\src\report.py' --out '$out'"
$action = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -WindowStyle Hidden -Command `"$cmd`""
$triggers = @(
  (New-ScheduledTaskTrigger -AtStartup),
  (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10))
)
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "TokenCounter" -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "TokenCounter"
# 4. OpenTelemetry (opcional): receptor local siempre activo + configuración gestionada para todos los usuarios
if (-not $NoOtel) {
  $oaction = New-ScheduledTaskAction -Execute $Python -Argument "-I `"$root\src\otel_receiver.py`"" -WorkingDirectory $root
  $osettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName "TokenCounterOtel" -Action $oaction -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $principal -Settings $osettings -Force | Out-Null
  Start-ScheduledTask -TaskName "TokenCounterOtel"
  & $Python -I "$root\src\managed_settings.py" apply
  Write-Host "OTel activo. Aplica solo a sesiones de Claude Code iniciadas a partir de ahora."
}
if ($Share) { Write-Host "Listo. Reporte: $out  |  Red: \\$env:COMPUTERNAME\$ShareName\report.html" }
else { Write-Host "Listo. Reporte: $out  (usa -Share para publicarlo también en red)" }
