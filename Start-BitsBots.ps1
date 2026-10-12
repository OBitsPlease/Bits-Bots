param(
    [switch]$SkipOpen
)

$ErrorActionPreference = "Stop"
$appDirectory = $PSScriptRoot
$python = Join-Path $appDirectory ".venv\Scripts\python.exe"
$dashboardUrl = "http://127.0.0.1:8765"

function Test-BitsBots {
    $statusCode = & curl.exe `
        --silent `
        --output NUL `
        --write-out "%{http_code}" `
        --max-time 2 `
        "$dashboardUrl/api/auth/setup"
    return $LASTEXITCODE -eq 0 -and $statusCode -eq "200"
}

if (-not (Test-Path $python)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "BitsBots could not find its Python environment at $python.",
        "BitsBots could not start",
        "OK",
        "Error"
    )
    exit 1
}

if (-not (Test-BitsBots)) {
    Start-Process `
        -FilePath $python `
        -ArgumentList "-m", "bitsbots.main" `
        -WorkingDirectory $appDirectory `
        -WindowStyle Hidden

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-BitsBots) {
            $ready = $true
            break
        }
    }

    if (-not $ready) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            "BitsBots did not start within 30 seconds. Open $appDirectory and check the application logs.",
            "BitsBots could not start",
            "OK",
            "Error"
        )
        exit 1
    }
}

if (-not $SkipOpen) {
    Start-Process $dashboardUrl
}
