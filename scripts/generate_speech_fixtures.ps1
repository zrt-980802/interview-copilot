param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '../data/speech-v2/audio'),
    [string]$CasesFile = (Join-Path $PSScriptRoot '../tests/fixtures/speech/audio_cases.json')
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speechFixtureDir = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force $speechFixtureDir | Out-Null
$speechCases = Get-Content -LiteralPath $CasesFile -Raw -Encoding UTF8 | ConvertFrom-Json
$speechSynth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speechSynth.SelectVoice('Microsoft Huihui Desktop')
    $speechSynth.Rate = -1
    $speechFormat = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    foreach ($speechCase in $speechCases) {
        $speechPartIndex = 0
        foreach ($speechPart in $speechCase.parts) {
            if ($speechPart -is [string]) {
                $speechFile = Join-Path $speechFixtureDir ($speechCase.name + '-' + $speechPartIndex + '.wav')
                $speechSynth.SetOutputToWaveFile($speechFile, $speechFormat)
                $speechSynth.Speak($speechPart)
                $speechSynth.SetOutputToNull()
                Write-Output $speechFile
            }
            $speechPartIndex++
        }
    }
} finally { $speechSynth.Dispose() }
