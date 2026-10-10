$snackProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $snackProjectDir
python app.py --port 8765
