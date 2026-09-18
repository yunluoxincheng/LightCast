# 从 SourceForge 的 mpv-player-windows/libmpv 目录下载并校验 x64 libmpv-2.dll。
#
# 被 .github/workflows/release.yml（正式发布）与 release-dryrun.yml（PR 预演）
# 共用：预演与发布执行同一份选择/校验逻辑，不会分叉。
#
# 背景（0.1.29~0.1.32 事故）：RSS 里 i686 构建更新时，只按「标题不含 v3 取
# 最新」会误选 32 位包，安装包内置后用户机器上启动即「libmpv 不可用」。
# 因此这里必须显式限定 x86_64，并在解压后校验 PE 头——文件存在不等于可加载。
param(
    [string] $Destination = "bin"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$xml = [xml](curl.exe -sL 'https://sourceforge.net/projects/mpv-player-windows/rss?path=/libmpv')
$items = $xml.SelectNodes('//rss/channel/item')
$chosen = $null
foreach ($it in $items) {
    $title = $it.SelectSingleNode('title').InnerText
    if ($title -like '*.7z' -and $title -like '*x86_64*') {
        if ($title -notlike '*v3*') { $chosen = $it; break }
        if (-not $chosen) { $chosen = $it }
    }
}
if (-not $chosen) { throw 'SourceForge libmpv 目录未找到 x86_64 的 .7z 包' }
$title = $chosen.SelectSingleNode('title').InnerText
$url = $chosen.SelectSingleNode('link').InnerText
Write-Host "下载: $title"
curl.exe -sL $url -o libmpv.7z
if (-not (Test-Path 'libmpv.7z')) { throw 'libmpv.7z 下载失败' }
& 'C:\Program Files\7-Zip\7z.exe' e libmpv.7z "-o$Destination" libmpv-2.dll -y
if (-not (Test-Path (Join-Path $Destination 'libmpv-2.dll'))) { throw '解压后未找到 libmpv-2.dll' }

# PE 头兜底校验：machine 必须是 0x8664（x64），非 x64 构建直接失败
$fs = [System.IO.File]::OpenRead((Join-Path $Destination 'libmpv-2.dll'))
try {
    $head = New-Object byte[] 4096
    [void]$fs.Read($head, 0, 4096)
} finally { $fs.Close() }
$peOffset = [BitConverter]::ToInt32($head, 0x3C)
$machine = [BitConverter]::ToUInt16($head, $peOffset + 4)
if ($machine -ne 0x8664) {
    throw "libmpv-2.dll 不是 x64 PE（machine=0x$($machine.ToString('X4'))），拒绝打包"
}
Write-Host "libmpv-2.dll 架构校验通过（x64）: $title"
