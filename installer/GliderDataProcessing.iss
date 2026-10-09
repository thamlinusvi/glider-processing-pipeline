; ============================================================
; Glider Data Processing — Inno Setup Installer
; Compile with Inno Setup 6 (https://jrsoftware.org/isinfo.php)
; Run the compiler from this file's location or open it in
; the Inno Setup IDE and press F9.
; ============================================================

#define AppName    "Glider Data Processing"
#define AppVersion "1.0"
#define AppPublisher "UVI"
#define AppExe     "start_app.bat"
#define AppRoot    ".."         ; repo root relative to this .iss file

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/thamlinusvi/glider-processing-pipeline
DefaultDirName=C:\GliderDataProcessing
DefaultGroupName={#AppName}
AllowNoIcons=no
OutputDir=.
OutputBaseFilename=GliderDataProcessing_Setup
SetupIconFile=icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Avoid Program Files — Docker bind-mounts and data writes need full permissions
PrivilegesRequired=admin
DisableDirPage=no
DirExistsWarning=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; ── Files ─────────────────────────────────────────────────────────────────────
; App code only — user data goes to the separately chosen data directory.
; Excluded: .git, node_modules (npm install handles these), __pycache__,
;           data dirs (deployments, ad2cp, etc.), gitignored credentials.
[Files]
; Core application
Source: "{#AppRoot}\electron_app\*";    DestDir: "{app}\electron_app";    Flags: recursesubdirs createallsubdirs; Excludes: "node_modules\*,__pycache__\*,*.pyc"
Source: "{#AppRoot}\app\*";             DestDir: "{app}\app";             Flags: recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc"
Source: "{#AppRoot}\gncutils\*";        DestDir: "{app}\gncutils";        Flags: recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc,*.egg-info\*"
Source: "{#AppRoot}\rugliderqc\*";      DestDir: "{app}\rugliderqc";      Flags: recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc,*.egg-info\*"
Source: "{#AppRoot}\slocum\*";          DestDir: "{app}\slocum";          Flags: recursesubdirs createallsubdirs
Source: "{#AppRoot}\qc\*";              DestDir: "{app}\qc";              Flags: recursesubdirs createallsubdirs
Source: "{#AppRoot}\master_cac\*";      DestDir: "{app}\master_cac";      Flags: recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "{#AppRoot}\config\*";          DestDir: "{app}\config";          Flags: recursesubdirs createallsubdirs; Excludes: "cloud\credentials.json,cloud\tokens.json"

; Root-level scripts and configs
Source: "{#AppRoot}\Dockerfile";              DestDir: "{app}"; Flags: skipifsourcedoesntexist
Source: "{#AppRoot}\requirements.txt";        DestDir: "{app}"
Source: "{#AppRoot}\start_app.bat";           DestDir: "{app}"
Source: "{#AppRoot}\update.bat";              DestDir: "{app}"
Source: "{#AppRoot}\*.py";                    DestDir: "{app}"; Flags: skipifsourcedoesntexist
Source: "{#AppRoot}\*.yml";                   DestDir: "{app}"; Flags: skipifsourcedoesntexist
Source: "{#AppRoot}\*.yaml";                  DestDir: "{app}"; Flags: skipifsourcedoesntexist
Source: "{#AppRoot}\CLAUDE.md";               DestDir: "{app}"; Flags: skipifsourcedoesntexist
Source: "{#AppRoot}\KNOWN_ISSUES.md";         DestDir: "{app}"; Flags: skipifsourcedoesntexist
Source: "{#AppRoot}\CHANGES_FROM_RUTGERS.md"; DestDir: "{app}"; Flags: skipifsourcedoesntexist

; Icon (for shortcuts)
Source: "icon.ico"; DestDir: "{app}\installer"

; ── Shortcuts ─────────────────────────────────────────────────────────────────
[Icons]
; Start Menu
Name: "{group}\{#AppName}";      Filename: "{app}\start_app.bat"; WorkingDir: "{app}"; IconFilename: "{app}\installer\icon.ico"
Name: "{group}\Update App";      Filename: "{app}\update.bat";    WorkingDir: "{app}"; IconFilename: "{app}\installer\icon.ico"
Name: "{group}\Uninstall";       Filename: "{uninstallexe}"
; Desktop
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\start_app.bat"; WorkingDir: "{app}"; IconFilename: "{app}\installer\icon.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

; ── Post-install commands ──────────────────────────────────────────────────────
[Run]
; npm install — installs Electron and Node deps
Filename: "cmd.exe"; Parameters: "/c npm install"; WorkingDir: "{app}\electron_app"; \
  StatusMsg: "Installing Node.js dependencies..."; Flags: runhidden waituntilterminated

; pip install — installs Python deps
Filename: "cmd.exe"; Parameters: "/c pip install -r requirements.txt"; WorkingDir: "{app}"; \
  StatusMsg: "Installing Python dependencies..."; Flags: runhidden waituntilterminated

; docker build — shown as optional checkbox; opens visible window so progress is visible
Filename: "cmd.exe"; Parameters: "/k docker build -t glider-processing . & pause"; WorkingDir: "{app}"; \
  Description: "Build Docker image (required for pipeline stages — takes a few minutes)"; \
  Flags: postinstall shellexec waituntilterminated; Check: DockerAvailable

; Launch app now
Filename: "{app}\start_app.bat"; WorkingDir: "{app}"; \
  Description: "Launch {#AppName} now"; Flags: postinstall shellexec nowait

; ── Pascal script ─────────────────────────────────────────────────────────────
[Code]

var
  DataDirPage: TInputDirWizardPage;

// ── Check that required tools are on PATH before allowing install ────────────
function InitializeSetup(): Boolean;
var
  RC:      Integer;
  Missing: String;
begin
  Missing := '';

  if not Exec(ExpandConstant('{sys}\cmd.exe'), '/c py --version >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, RC) or (RC <> 0) then
    if not Exec(ExpandConstant('{sys}\cmd.exe'), '/c python --version >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, RC) or (RC <> 0) then
      Missing := Missing + '  • Python 3.10+    https://www.python.org/downloads/' + #13#10;

  if not Exec(ExpandConstant('{sys}\cmd.exe'), '/c node --version >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, RC) or (RC <> 0) then
    Missing := Missing + '  • Node.js 24+      https://nodejs.org/' + #13#10;

  if not Exec(ExpandConstant('{sys}\cmd.exe'), '/c git --version >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, RC) or (RC <> 0) then
    Missing := Missing + '  • Git              https://git-scm.com/' + #13#10;

  if not Exec(ExpandConstant('{sys}\cmd.exe'), '/c docker --version >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, RC) or (RC <> 0) then
    Missing := Missing + '  • Docker Desktop   https://www.docker.com/products/docker-desktop/' + #13#10;

  if Missing <> '' then
  begin
    MsgBox(
      'The following prerequisites must be installed before continuing:' + #13#10#13#10 +
      Missing + #13#10 +
      'Install them, ensure Docker Desktop is running, then re-run this installer.',
      mbError, MB_OK
    );
    Result := False;
  end else
    Result := True;
end;

// ── Data directory wizard page ────────────────────────────────────────────────
procedure InitializeWizard;
begin
  DataDirPage := CreateInputDirPage(
    wpSelectDir,
    'Select Data Directory',
    'Where should glider processing data be stored?',
    'The data directory holds all files created during processing:' + #13#10 +
    #13#10 +
    '  deployments\       — per-deployment processed NetCDF, QC output, plots' + #13#10 +
    '  dat_files\         — intermediate .dat files from glider binary conversion' + #13#10 +
    '  incoming_data\     — raw .dbd / .ebd glider binary files to be processed' + #13#10 +
    '  ad2cp\             — AD2CP (ADCP) raw and processed data' + #13#10 +
    '  logs\              — pipeline run logs' + #13#10 +
    #13#10 +
    'This can be on a different drive from the application.' + #13#10 +
    'Choose a location with plenty of free space (deployments can be many GB each).',
    False,
    'New Folder'
  );
  DataDirPage.Add('');
  DataDirPage.Values[0] := 'C:\GliderData';
end;

// ── After files are extracted, write config and create data directories ───────
procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir:     String;
  EscapedDir:  String;
  ConfigPath:  String;
  JSON:        String;
begin
  if CurStep = ssPostInstall then
  begin
    DataDir    := DataDirPage.Values[0];
    EscapedDir := DataDir;
    StringChange(EscapedDir, '\', '\\');

    ConfigPath := ExpandConstant('{app}\electron_app\glider_config.json');
    JSON       := '{"dataDir": "' + EscapedDir + '"}';
    SaveStringToFile(ConfigPath, JSON, False);

    // Create data subdirectories
    ForceDirectories(DataDir + '\deployments');
    ForceDirectories(DataDir + '\dat_files');
    ForceDirectories(DataDir + '\incoming_data');
    ForceDirectories(DataDir + '\ad2cp');
    ForceDirectories(DataDir + '\ad2cp\tests');
    ForceDirectories(DataDir + '\logs');
    ForceDirectories(DataDir + '\gncutils_slocum_nc_output');
    ForceDirectories(DataDir + '\post_processing');

    // Remind user about credentials
    MsgBox(
      'Data directory created at:' + #13#10 +
      '  ' + DataDir + #13#10#13#10 +
      'IMPORTANT — one manual step required:' + #13#10 +
      'Copy your cloud credentials file to:' + #13#10 +
      '  ' + ExpandConstant('{app}') + '\config\cloud\credentials.json' + #13#10#13#10 +
      'Without it, cloud upload (Google Drive / Dropbox) will not work.' + #13#10 +
      'All other features work without it.',
      mbInformation, MB_OK
    );
  end;
end;

// ── Used by [Run] Check: to show Docker build only if Docker is available ─────
function DockerAvailable: Boolean;
var RC: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\cmd.exe'), '/c docker --version >nul 2>&1',
                 '', SW_HIDE, ewWaitUntilTerminated, RC) and (RC = 0);
end;
