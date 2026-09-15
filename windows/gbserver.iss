; gbserver Windows installer (Inno Setup 6)
;
; Packages the finished one-folder build from:
;     windows\build_exe.cmd
;   which produces  dist\gbserver\  (gbserver.exe + _internal\ + roms\ + saves\)
; into a single installer:  windows\Output\gbserver-setup.exe
;
; Compile with Inno Setup 6 (https://jrsoftware.org/isdl.php):
;   - open this file in Inno Setup and press Compile, or
;   - run:  windows\build_installer.cmd
;
; Installs per-user (no administrator needed) under
; %LOCALAPPDATA%\Programs\gbserver, adds Start Menu and Desktop
; shortcuts, and registers a normal uninstaller.

#define MyAppName "gbserver"
#define MyAppVersion "1.0"
#define MyAppExeName "gbserver.exe"

[Setup]
AppId={{4E4C8A2A-9B7F-4B2E-8D14-6F1C3A5E20D1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
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

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The whole dist\gbserver folder (exe, _internal\, roms\, saves\).
Source: "..\dist\gbserver\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\gbserver"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\gbserver dashboard"; Filename: "http://127.0.0.1:8080/dashboard"
Name: "{autodesktop}\gbserver"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{autodesktop}\gbserver dashboard"; Filename: "http://127.0.0.1:8080/dashboard"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Run gbserver now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean anything added at runtime inside the app folder (roms\ and saves\
; grow after install), then remove the app dir once empty.
Type: filesandordirs; Name: "{app}\roms"
Type: filesandordirs; Name: "{app}\saves"
Type: dirifempty; Name: "{app}"