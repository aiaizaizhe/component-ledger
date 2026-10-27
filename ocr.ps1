param([Parameter(Mandatory = $true)][string]$Path)

# Offline OCR using the OCR engine built into Windows 10/11.
# Prints one line of JSON: {"ok":true,"engine":"zh-Hans-CN","words":[...]}
# This file is ASCII-only on purpose: cmd/PowerShell encoding issues.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Fail([string]$Message) {
    @{ ok = $false; error = $Message } | ConvertTo-Json -Compress
    exit 0
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

    function Await($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        $netTask.Result
    }

    [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null

    $engine = $null
    foreach ($tag in @("zh-Hans-CN", "zh-Hans", "zh-CN")) {
        try {
            $lang = [Windows.Globalization.Language]::new($tag)
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
        } catch {
            $engine = $null
        }
        if ($engine) { break }
    }
    if (-not $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if (-not $engine) { Write-Fail "no-ocr-engine" }

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

    $words = New-Object System.Collections.ArrayList
    foreach ($line in $result.Lines) {
        foreach ($w in $line.Words) {
            [void]$words.Add(@{
                t = $w.Text
                x = [math]::Round($w.BoundingRect.X, 1)
                y = [math]::Round($w.BoundingRect.Y, 1)
                w = [math]::Round($w.BoundingRect.Width, 1)
                h = [math]::Round($w.BoundingRect.Height, 1)
            })
        }
    }

    @{
        ok = $true
        engine = $engine.RecognizerLanguage.LanguageTag
        words = @($words)
    } | ConvertTo-Json -Depth 4 -Compress
} catch {
    Write-Fail ($_.Exception.Message)
}
