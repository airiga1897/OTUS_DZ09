function Install-WpCli {
    param([string]$Root)
    $config = Get-Content -Raw -LiteralPath (Join-Path $Root 'config/wp-cli.json') | ConvertFrom-Json
    if ($config.version -notmatch '^\d+\.\d+\.\d+$' -or $config.sha512 -notmatch '^[a-f0-9]{128}$') {
        throw 'Некорректная конфигурация WP-CLI.'
    }
    $directory = Join-Path $Root '.tools/wp-cli'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $destination = Join-Path $directory "wp-cli-$($config.version).phar"
    if (Test-Path -LiteralPath $destination) {
        if ((Get-FileHash -LiteralPath $destination -Algorithm SHA512).Hash -eq $config.sha512) { return }
        Remove-Item -LiteralPath $destination -Force
    }
    $temporary = Join-Path $directory "$([guid]::NewGuid()).part"
    $url = "https://github.com/wp-cli/wp-cli/releases/download/v$($config.version)/wp-cli-$($config.version).phar"
    try {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                Invoke-WebRequest -Uri $url -OutFile $temporary -TimeoutSec 60 -UseBasicParsing -ErrorAction Stop
                if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA512).Hash -ne $config.sha512) {
                    throw 'Контрольная сумма WP-CLI не совпадает.'
                }
                Move-Item -LiteralPath $temporary -Destination $destination
                return
            } catch {
                if ($attempt -eq 3) { throw }
                Start-Sleep -Seconds 2
            }
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}
