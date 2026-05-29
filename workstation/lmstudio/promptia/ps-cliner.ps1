param (
    [string]$hostw = "localhost",
    [int]$portw = 8080,
    [string]$ApiKey = "localm",
    [int]$MaxTurns = 7
)

$BaseUrl = "http://${hostw}:${portw}/v1"

Write-Host "🚀 Initializing Native PowerShell Agent..." -ForegroundColor Cyan

# --- 1. Fetch Available Model ---
$ModelName = $null
try {
    $modelsResponse = Invoke-RestMethod -Uri "$BaseUrl/models" -Method Get -TimeoutSec 10
    if ($modelsResponse.models.Count -gt 0) {
        $ModelName = $modelsResponse.models[0].name
        Write-Host "✅ Ready: $ModelName" -ForegroundColor Green
    } else {
        Write-Host "⚠️ Warning: No models found in API response." -ForegroundColor Yellow
        exit
    }
} catch {
    Write-Host "❌ ERROR: Failed to connect to local API at $BaseUrl" -ForegroundColor Red
    exit
}

# --- 2. System Prompt ---
$SystemPrompt = @"
You are an autonomous PowerShell agent. Your goal is to fulfill the User Intent, verify it actually worked, and report the final status.

RULES:
1. You operate in a loop. You can write inline, compact PowerShell code to execute/verify something natively, OR declare the task finished.
2. The code runs in the same persistent PowerShell session across turns. Variables you define (e.g., `$var = "data"`) ARE available in the next turn.
3. Do not declare SUCCESS unless you have explicit proof from stdout/stderr. Output results explicitly so you can see them.
4. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{
  "action_type": "run_powershell" or "declare_result",
  "code": "<powershell code string> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation for the result> or null"
}
"@

# --- 3. Execution Function ---
function Invoke-AgentCode {
    param([string]$Code)

    Write-Host "`n[SYSTEM] Executing PowerShell:" -ForegroundColor Cyan
    Write-Host $Code -ForegroundColor DarkYellow

    $stdout = [System.Text.StringBuilder]::new()
    $stderr = [System.Text.StringBuilder]::new()
    $exitCode = 0

    try {
        # 2>&1 merges the error stream into the success stream so we can catch everything
        $rawOutput = Invoke-Expression $Code 2>&1

        $successObjects = @()

        foreach ($item in $rawOutput) {
            # Separate errors from standard output
            if ($item -is [System.Management.Automation.ErrorRecord]) {
                [void]$stderr.AppendLine($item.ToString())
            } elseif ($null -ne $item) {
                $successObjects += $item
            }
        }

        # Pipe all standard output objects to Out-String at once to format tables correctly
        if ($successObjects.Count -gt 0) {
            $formattedOutput = $successObjects | Out-String
            [void]$stdout.Append($formattedOutput)
        }

        $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    } catch {
        [void]$stderr.AppendLine($_.Exception.Message)
        $exitCode = 1
    }

    return @{
        stdout = $stdout.ToString().Trim()
        stderr = $stderr.ToString().Trim()
        exit_code = $exitCode
    }
}

# --- 4. Main Agent Loop ---
function Start-AgentLoop {
    param([string]$UserIntent)

    $Messages = @(
        @{ role = "system"; content = $SystemPrompt },
        @{ role = "user"; content = "User Intent: $UserIntent" }
    )

    for ($turn = 1; $turn -le $MaxTurns; $turn++) {
        Write-Host "`n---- Turn $turn ----" -ForegroundColor Cyan -NoNewline
        Write-Host " (Thinking...)" -ForegroundColor DarkGray

        $Payload = @{
            model = $ModelName
            messages = $Messages
            temperature = 0.0
            response_format = @{ type = "json_object" }
        } | ConvertTo-Json -Depth 10

        try {
            $Response = Invoke-RestMethod -Uri "$BaseUrl/chat/completions" -Method Post -Body $Payload -ContentType "application/json" -Headers @{ "Authorization" = "Bearer $ApiKey" } -TimeoutSec 120
            
            $RawOutput = $Response.choices[0].message.content
            
            # Clean potential Markdown wrap from LLM
            $CleanJson = $RawOutput -replace '```json', '' -replace '```', ''
            $ParsedAction = $CleanJson | ConvertFrom-Json

            # Save assistant response to history
            $Messages += @{ role = "assistant"; content = ($ParsedAction | ConvertTo-Json -Compress) }

            if ($ParsedAction.action_type -eq "declare_result") {
                Write-Host "`n❇  [FINAL RESULT] $($ParsedAction.status)" -ForegroundColor Green
                Write-Host "🧠 [REASON] $($ParsedAction.reason)" -ForegroundColor Magenta
                return
            } 
            elseif ($ParsedAction.action_type -eq "run_powershell") {
                if ([string]::IsNullOrWhiteSpace($ParsedAction.code)) {
                    Write-Host "`n❌ [ERROR] LLM requested execution but provided no code." -ForegroundColor Red
                    break
                }

                $ExecResult = Invoke-AgentCode -Code $ParsedAction.code
                
                if ($ExecResult.stdout) { Write-Host "[STDOUT]`n$($ExecResult.stdout)" }
                if ($ExecResult.stderr) { Write-Host "[STDERR]`n$($ExecResult.stderr)" -ForegroundColor Red }
                if (-not $ExecResult.stdout -and -not $ExecResult.stderr) { Write-Host "[OUTPUT] (No standard output or error)" -ForegroundColor DarkGray }
                
                Write-Host "[EXIT CODE] $($ExecResult.exit_code)" -ForegroundColor DarkGray

                $Evidence = "Code Executed:`n$($ParsedAction.code)`nExit Code: $($ExecResult.exit_code)`nstdout: $($ExecResult.stdout)`nstderr: $($ExecResult.stderr)"
                $Messages += @{ role = "user"; content = $Evidence }
            } else {
                Write-Host "`n❌ [ERROR] Unknown action type: $($ParsedAction.action_type)" -ForegroundColor Red
                break
            }
        } catch {
            Write-Host "`n❌ API Request Failed: $($_.Exception.Message)" -ForegroundColor Red
            break
        }
    }
    
    if ($turn -gt $MaxTurns) {
        Write-Host "`n⚠️ [WARNING] Max turns reached. Agent loop terminated." -ForegroundColor Yellow
    }
}

# --- Interactive Prompt ---
while ($true) {
    Write-Host "`nPS-CLINER> " -ForegroundColor Blue -NoNewline
    $InputIntent = Read-Host
    if ($InputIntent -match '^(exit|quit)$') { break }
    if (-not [string]::IsNullOrWhiteSpace($InputIntent)) {
        Start-AgentLoop -UserIntent $InputIntent
    }
}
