; gbserver auto-updating Windows installer (Inno Setup 6)
;
; Build with:  windows\build_installer.cmd
;
; Installs the runtime (gbserver.exe + _internal\) and an offline snapshot of
; the code to %LOCALAPPDATA%\Programs\gbserver. On launch, gbserver.exe keeps
; the code up to date from GitHub. ROMs and saves live in
; %LOCALAPPDATA%\gbserver\data and are never removed by updates, reinstalls
; or uninstalling.

#define MyAppName "gbserver"
#ifndef RuntimeVersion
  #define RuntimeVersion "1"
#endif
#define MyAppExeName "gbserver.exe"

[Setup]
AppId={{4E4C8A2A-9B7F-4B2E-8D14-6F1C3A5E20D1}
AppName={#MyAppName}
AppVersion=runtime {#RuntimeVersion}
AppPublisher=wulfpax-labs
DefaultDirName={localappdata}\Programs\gbserver
DefaultGroupName=gbserver
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=gbserver-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\gbserver.exe
UninstallDisplayName=gbserver
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
; Replace the runtime cleanly so libraries from an older build can't linger.
; ROMs and saves from builds that kept them here are moved to
; %LOCALAPPDATA%\gbserver\data by gbserver.exe on first launch, so those
; folders are deliberately left alone.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\seed"

[Files]
Source: "..\dist\gbserver\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
Name: "{localappdata}\gbserver\data\roms"; Flags: uninsneveruninstall
Name: "{localappdata}\gbserver\data\saves"; Flags: uninsneveruninstall

[Icons]
Name: "{autoprograms}\gbserver"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\gbserver dashboard"; Filename: "http://127.0.0.1:8080/dashboard"
Name: "{autoprograms}\gbserver ROMs and saves"; Filename: "{localappdata}\gbserver\data"
Name: "{autoprograms}\gbserver update log"; Filename: "{localappdata}\gbserver\launcher.log"
Name: "{autodesktop}\gbserver"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Run gbserver now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Downloaded code versions are disposable; ROMs, saves and settings are kept.
Type: filesandordirs; Name: "{localappdata}\gbserver\app"
Type: dirifempty; Name: "{app}"
