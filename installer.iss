; Inno Setup script for copy-save. Built by build.py:
;   ISCC /DAppVersion=1.0.0 installer.iss
; Expects PyInstaller's output in dist\copy-save\.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Fixed ID so upgrades replace the existing install. Never change it.
AppId={{7C3E2B9A-4F61-4D8E-9B2A-6A1F0C5D8E31}
AppName=copy-save
AppVersion={#AppVersion}
AppVerName=copy-save {#AppVersion}
AppPublisher=copy-save
DefaultDirName={autopf}\copy-save
DefaultGroupName=copy-save
DisableProgramGroupPage=yes
; Per-user install by default (no admin prompt); users can still pick "all users".
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=copy-save-{#AppVersion}-setup
SetupIconFile=copy_save.ico
UninstallDisplayIcon={app}\copy-save.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Close a running copy-save before upgrading/uninstalling
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\copy-save\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID matches the app's, so the taskbar groups and pins it correctly.
Name: "{autoprograms}\copy-save"; Filename: "{app}\copy-save.exe"; AppUserModelID: "copy-save"
Name: "{autodesktop}\copy-save"; Filename: "{app}\copy-save.exe"; AppUserModelID: "copy-save"; Tasks: desktopicon

[Run]
Filename: "{app}\copy-save.exe"; Description: "{cm:LaunchProgram,copy-save}"; Flags: nowait postinstall skipifsilent

; Settings in %APPDATA%\copy-save and the backup zips are deliberately left
; in place on uninstall.
