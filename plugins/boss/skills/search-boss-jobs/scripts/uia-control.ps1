param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("inspect", "invoke")]
    [string]$Command,
    [string]$Name = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class BossWindowState {
    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}
"@
if ([string]::IsNullOrEmpty($Name)) {
    $Name = -join ([char]0x53D1, [char]0x9001)
}

$targets = [System.Collections.Generic.List[object]]::new()
$seenTargets = [System.Collections.Generic.HashSet[string]]::new()
$chromeProcesses = Get-Process chrome -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 }

# UIA does not expose Chromium's web controls while its only top-level window
# is minimized. Restore only the uniquely identified BOSS Chrome window; do
# not activate it, move the pointer, or invoke a control here.
$bossWindows = @($chromeProcesses | Where-Object { $_.MainWindowTitle -match 'BOSS直聘' })
if ($bossWindows.Count -eq 1) {
    $bossHandle = [IntPtr]$bossWindows[0].MainWindowHandle
    if ([BossWindowState]::IsIconic($bossHandle)) {
        [void][BossWindowState]::ShowWindowAsync($bossHandle, 9)
        Start-Sleep -Milliseconds 500
    }
}

foreach ($process in $chromeProcesses) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle(
        [IntPtr]$process.MainWindowHandle
    )
    if (-not $root) { continue }
    $nameCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $Name
    )
    $elements = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        $nameCondition
    )
    for ($index = 0; $index -lt $elements.Count; $index++) {
        $element = $elements.Item($index)
        $target = $null
        $source = ""
        $patternObject = $null
        $supportsInvoke = $element.TryGetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern,
            [ref]$patternObject
        )
        if (
            $element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and
            $element.Current.IsEnabled -and
            -not $element.Current.IsOffscreen -and
            $supportsInvoke
        ) {
            $target = $element
            $source = "exact_named_button"
        }
        elseif (
            $element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
            -not $element.Current.IsOffscreen
        ) {
            # BOSS's detail-page modal exposes the visible label as Text and
            # its clickable parent as an unnamed Group.  Accept only that
            # narrowly identified semantic parent; never invoke a generic
            # ancestor or use its screen coordinates.
            $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
            $parent = $walker.GetParent($element)
            if ($parent) {
                $parentPattern = $null
                $parentSupportsInvoke = $parent.TryGetCurrentPattern(
                    [System.Windows.Automation.InvokePattern]::Pattern,
                    [ref]$parentPattern
                )
                $parentClass = $parent.Current.ClassName
                if (
                    $parent.Current.ControlType -eq [System.Windows.Automation.ControlType]::Group -and
                    $parentClass -match '(^|\s)send-message(\s|$)' -and
                    $parentClass -notmatch '(^|\s)disable(\s|$)' -and
                    $parent.Current.IsEnabled -and
                    -not $parent.Current.IsOffscreen -and
                    $parentSupportsInvoke
                ) {
                    $target = $parent
                    $source = "modal_send_parent"
                }
            }
        }
        if ($target) {
            $runtimeId = $target.GetRuntimeId() -join "."
            if (-not $seenTargets.Add("$($process.Id):$runtimeId")) { continue }
            $targets.Add([PSCustomObject]@{
                ProcessId = $process.Id
                WindowTitle = $root.Current.Name
                Name = $Name
                ControlType = $target.Current.ControlType.ProgrammaticName
                Enabled = $target.Current.IsEnabled
                Offscreen = $target.Current.IsOffscreen
                Rectangle = $target.Current.BoundingRectangle.ToString()
                Source = $source
                Element = $target
            })
        }
    }
}

if ($Command -eq "inspect") {
    $safe = $targets | Select-Object ProcessId, WindowTitle, Name, ControlType, Enabled, Offscreen, Rectangle, Source
    [PSCustomObject]@{
        ok = $true
        count = $targets.Count
        matches = @($safe)
    } | ConvertTo-Json -Depth 5
    exit 0
}

if ($targets.Count -ne 1) {
    [PSCustomObject]@{
        ok = $false
        error = "expected_one_unique_enabled_invoke_button"
        count = $targets.Count
    } | ConvertTo-Json -Compress
    exit 2
}

$target = $targets[0].Element
$invoke = [System.Windows.Automation.InvokePattern]$target.GetCurrentPattern(
    [System.Windows.Automation.InvokePattern]::Pattern
)
$invoke.Invoke()
[PSCustomObject]@{
    ok = $true
    action = "invoke"
    name = $Name
    process_id = $targets[0].ProcessId
    window_title = $targets[0].WindowTitle
    source = $targets[0].Source
} | ConvertTo-Json -Compress
