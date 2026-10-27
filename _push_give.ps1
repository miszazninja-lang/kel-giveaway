# Заливка открытого репозитория «Честный розыгрыш» на GitHub (публичный: github.com/miszazninja-lang/kel-giveaway).
# Перед первым запуском создай ПУСТОЙ публичный репозиторий kel-giveaway на github.com/new (без README).
# Свежие файлы бота берутся из рабочей копии Кель — тот же код, что работает на сервере.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$src = [IO.Path]::GetFullPath((Join-Path $root '..\сенку\senkuro_mcp'))
$repoUrl = 'https://github.com/miszazninja-lang/kel-giveaway.git'

foreach ($f in @('give_core.py', 'kel_give.py', 'give_verify.js', 'give_page.html')) {
  Copy-Item -LiteralPath (Join-Path $src $f) -Destination (Join-Path $root $f) -Force
}
$py = Join-Path $src '.venv\Scripts\python.exe'
& $py (Join-Path $root 'tests\test_core.py')
if ($LASTEXITCODE -ne 0) { Write-Host 'ЗАЛИВКА ОСТАНОВЛЕНА: не прошла проверка формулы.' -ForegroundColor Red; exit 1 }

Set-Location -LiteralPath $root
if (-not (Test-Path -LiteralPath (Join-Path $root '.git'))) {
  git init -q -b main
  git remote add origin $repoUrl
}
# автор — без личной почты: репозиторий публичный
git config user.name 'miszazninja-lang'
git config user.email 'miszazninja-lang@users.noreply.github.com'
git add -A
git -c core.quotepath=false status --short
if (git status --porcelain) {
  git commit -q -m ('Честный розыгрыш ' + (Get-Date -Format 'yyyy-MM-dd HH:mm'))
}
git push -u origin main
if ($LASTEXITCODE -ne 0) {
  Write-Host 'Не отправилось. Проверь, что репозиторий kel-giveaway создан на GitHub (пустой, публичный).' -ForegroundColor Yellow
  exit 1
}
Write-Host 'Готово: https://github.com/miszazninja-lang/kel-giveaway' -ForegroundColor Green
