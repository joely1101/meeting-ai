#define MyAppName "會議錄音器"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "Meeting AI"
#define MyAppExeName "會議錄音.exe"

[Setup]
AppId={{8B3E1F9A-4C2D-4E1B-9A3F-5D9E2F8A1C08}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\MeetingRecorder
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 免管理員權限即可安裝，避免 UAC 彈窗警告
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist_installer
OutputBaseFilename=會議錄音_安裝檔
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Tasks]
Name: "desktopicon"; Description: "建立桌面捷徑"; GroupDescription: "額外圖示:"

[Files]
Source: "..\web\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\解除安裝 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即啟動 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  SetupExePath, AppExePath, CfgPath, AppDataCfgPath: String;
  FS: TFileStream;
  Buf: AnsiString;
  FileSize: Int64;
  PStart, PEnd: Integer;
  ServerUrl, CfgContent: String;
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    AppExePath := ExpandConstant('{app}\{#MyAppExeName}');
    CfgPath := ExpandConstant('{app}\client_config.json');
    AppDataCfgPath := ExpandConstant('{userappdata}\MeetingRecorder\client_config.json');

    // 1. 自動解除 Windows 下載封鎖 (Mark of the Web)
    Exec('powershell.exe', '-NoProfile -Command "Unblock-File -Path ''' + AppExePath + ''' -ErrorAction SilentlyContinue"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // 2. 嘗試從安裝檔尾端讀取注入的 Server URL Overlay
    SetupExePath := ExpandConstant('{srcexe}');
    if (SetupExePath <> '') and FileExists(SetupExePath) then
    begin
      try
        FS := TFileStream.Create(SetupExePath, fmOpenRead or fmShareDenyNone);
        try
          FileSize := FS.Size;
          if FileSize > 8192 then
          begin
            FS.Seek(FileSize - 8192, soFromBeginning);
            SetLength(Buf, 8192);
            FS.Read(Buf[1], 8192);
          end
          else
          begin
            SetLength(Buf, FileSize);
            FS.Read(Buf[1], FileSize);
          end;

          PStart := Pos('__SERVER_URL_OVERLAY__:', String(Buf));
          if PStart > 0 then
          begin
            PStart := PStart + Length('__SERVER_URL_OVERLAY__:');
            PEnd := Pos(':__END__', Copy(String(Buf), PStart, Length(Buf) - PStart + 1));
            if PEnd > 0 then
            begin
              ServerUrl := Copy(String(Buf), PStart, PEnd - 1);
              ServerUrl := Trim(ServerUrl);
              if (Copy(ServerUrl, 1, 7) = 'http://') or (Copy(ServerUrl, 1, 8) = 'https://') then
              begin
                CfgContent := '{' + #13#10 +
                  '  "server_url": "' + ServerUrl + '",' + #13#10 +
                  '  "token": "",' + #13#10 +
                  '  "user_email": "",' + #13#10 +
                  '  "user_name": "",' + #13#10 +
                  '  "auto_upload": true' + #13#10 +
                  '}';
                // 寫入安裝目錄
                SaveStringToFile(CfgPath, CfgContent, False);
                // 同步寫入 AppData 目錄
                ForceDirectories(ExpandConstant('{userappdata}\MeetingRecorder'));
                SaveStringToFile(AppDataCfgPath, CfgContent, False);
              end;
            end;
          end;
        finally
          FS.Free;
        end;
      except
        // 若無法讀取則忽略
      end;
    end;
  end;
end;
