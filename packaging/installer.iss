; 轻投（LightCast）安装包脚本（Inno Setup 6）
;
; 用法：
;   1. 先构建 exe:        pyinstaller LightCast.spec --noconfirm
;   2. 用 ISCC 编译本脚本: ISCC.exe packaging\installer.iss
;      （ISCC 在 Inno Setup 安装目录下，https://jrsoftware.org/isinfo.php）
;   3. 产物: dist\LightCast-Setup-0.1.0.exe
;
; 版本号可在命令行覆盖（GitHub Actions 发布时用）：
;   ISCC.exe /DMyAppVersion=1.2.0 packaging\installer.iss
;
; 注意：安装版把 PyInstaller 的 onedir 整个拷入安装目录，
; 配置/日志由应用自身写入 %APPDATA%\LightCast，不在安装目录内。

#define MyAppName "轻投"
#define MyAppNameEn "LightCast"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#define MyAppExeName "LightCast.exe"

[Setup]
AppId={{A7B9C2D3-4E5F-4A6B-8C9D-0E1F2A3B4C5D}
AppName={#MyAppName}（{#MyAppNameEn}）
AppVersion={#MyAppVersion}
AppPublisher={#MyAppNameEn}
DefaultDirName={autopf}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename={#MyAppNameEn}-Setup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}（{#MyAppNameEn}）
; 升级/覆盖安装时自动关闭正在运行的轻投（否则 exe 被锁定，旧文件残留，
; 表现为「装了新版但界面没变化」），安装完不自动重启应用
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no

[InstallDelete]
; onedir 升级：先清空 _internal（第三方库目录），避免新旧文件混杂
; （应用自身模块在 exe 内的 PYZ 归档里，会被 CloseApplications + ignoreversion 正常替换）
Type: filesandordirs; Name: "{app}\_internal"

[Languages]
; 中文翻译不随 Inno Setup 安装包附带，仓库内置了官方文件
; （languages/ChineseSimplified.isl，来自 jrsoftware/issrc），
; 用相对路径引用保证 CI/本地构建都不依赖安装环境
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppNameEn}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
