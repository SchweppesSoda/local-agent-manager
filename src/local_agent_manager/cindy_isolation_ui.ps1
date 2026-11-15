param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class CindyImportUI {
 [StructLayout(LayoutKind.Sequential)] public struct RECT {public int Left,Top,Right,Bottom;}
 [StructLayout(LayoutKind.Sequential)] public struct MI {public int dx,dy; public uint mouseData,dwFlags,time; public UIntPtr extra;}
 [StructLayout(LayoutKind.Sequential)] public struct KI {public ushort vk,scan; public uint flags,time; public UIntPtr extra;}
 [StructLayout(LayoutKind.Explicit)] public struct UNION {[FieldOffset(0)]public MI mouse;[FieldOffset(0)]public KI key;}
 [StructLayout(LayoutKind.Sequential)] public struct INPUT {public uint type;public UNION data;}
 [DllImport("user32.dll")]public static extern bool SetProcessDPIAware();
 [DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")]public static extern bool GetWindowRect(IntPtr h,out RECT r);
 [DllImport("user32.dll")]public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("user32.dll")]public static extern bool SetCursorPos(int x,int y);
 [DllImport("user32.dll")]public static extern void mouse_event(uint f,uint x,uint y,uint d,UIntPtr e);
 [DllImport("user32.dll",SetLastError=true)]public static extern uint SendInput(uint n,INPUT[] inputs,int size);
 public static void Text(string s){foreach(char c in s){
  INPUT[] v=new INPUT[2];v[0].type=1;v[0].data.key.scan=c;v[0].data.key.flags=4;
  v[1]=v[0];v[1].data.key.flags=6;
  if(SendInput(2,v,Marshal.SizeOf(typeof(INPUT)))!=2)throw new Exception("Text input unverified");}}
 public static void Wheel(int d){mouse_event(0x800,0,0,unchecked((uint)d),UIntPtr.Zero);}
}
'@
[void][CindyImportUI]::SetProcessDPIAware()
$req=Get-Content -LiteralPath $Request -Raw -Encoding UTF8|ConvertFrom-Json
if($req.action -notin @('observe','click','focus_composer','scroll','type_probe','submit_probe')){throw 'UI action not permitted'}
$processes=@(Get-Process Cindy -ErrorAction Stop)
foreach($p in $processes){if($p.Path -ine $req.executable){throw 'Unexpected Cindy image'}}
$windows=[System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
$targets=@($windows|Where-Object{$_.Current.ProcessId -in $processes.Id -and $_.Current.Name -eq 'Cindy'})
if($targets.Count -ne 1){throw 'Unique Cindy window not proved'}
$w=$targets[0];$h=[intptr]$w.Current.NativeWindowHandle
$p=Get-Process -Id $w.Current.ProcessId -ErrorAction Stop
$created=$p.StartTime.ToUniversalTime().Ticks.ToString()
if($req.expected){
 if($h.ToInt64() -ne $req.expected.hwnd -or $p.Id -ne $req.expected.pid -or $created -ne $req.expected.created){throw 'UI window identity changed'}
}
[void][CindyImportUI]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 150
if([CindyImportUI]::GetForegroundWindow() -ne $h){throw 'Cindy foreground not proved'}
$rect=New-Object CindyImportUI+RECT
if(![CindyImportUI]::GetWindowRect($h,[ref]$rect)){throw 'Window bounds unavailable'}
$width=$rect.Right-$rect.Left;$height=$rect.Bottom-$rect.Top
if($width -lt 100 -or $height -lt 100){throw 'Product window not ready'}
if($req.expected -and ($width -ne $req.expected.width -or $height -ne $req.expected.height -or $rect.Left -ne $req.expected.left -or $rect.Top -ne $req.expected.top)){throw 'Window moved; reobserve'}
if($req.action -in @('click','focus_composer','scroll','submit_probe')){
 if($req.x -le 0 -or $req.y -le 0 -or $req.x -ge $width -or $req.y -ge $height){throw 'Input outside observed product'}
 [void][CindyImportUI]::SetCursorPos($rect.Left+[int]$req.x,$rect.Top+[int]$req.y)
 if($req.action -eq 'scroll'){
  if([Math]::Abs([int]$req.delta) -gt 1200){throw 'Scroll exceeds fixed bound'}
  [CindyImportUI]::Wheel([int]$req.delta)
 }else{
  [CindyImportUI]::mouse_event(2,0,0,0,[uintptr]::Zero)
  [CindyImportUI]::mouse_event(4,0,0,0,[uintptr]::Zero)
 }
}elseif($req.action -eq 'type_probe'){
 $prefix=-join @([char]0x9694,[char]0x79bb,[char]0x9a8c,[char]0x6536,[char]0x6d4b,[char]0x8bd5,[char]0x20)
 if(!$req.text.StartsWith($prefix)-or $req.text.Length -gt 200){throw 'Only fixed isolation probe may be typed'}
 [CindyImportUI]::Text($req.text)
}
if($req.action -ne 'observe'){Start-Sleep -Milliseconds 250}
if([CindyImportUI]::GetForegroundWindow() -ne $h){throw 'Input changed foreground; outcome unknown'}
$bmp=New-Object System.Drawing.Bitmap $width,$height
$g=[System.Drawing.Graphics]::FromImage($bmp)
try{$g.CopyFromScreen($rect.Left,$rect.Top,0,0,$bmp.Size);$bmp.Save($req.screenshot,[System.Drawing.Imaging.ImageFormat]::Png)}finally{$g.Dispose();$bmp.Dispose()}
$focused=[System.Windows.Automation.AutomationElement]::FocusedElement
$focus=if($focused){@{pid=$focused.Current.ProcessId;type=$focused.Current.ControlType.ProgrammaticName;class=$focused.Current.ClassName;automationId=$focused.Current.AutomationId}}else{$null}
if($focus -and $focus.pid -notin $processes.Id){throw 'Focused control outside Cindy'}
$controls=@();$all=$w.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
for($i=0;$i -lt [Math]::Min($all.Count,500);$i++){
 $e=$all[$i].Current;$name=$e.Name
 if($name -and $name.Length -gt 160){$name=$name.Substring(0,160)}
 $controls+=@{index=$i;type=$e.ControlType.ProgrammaticName;name=$name;automationId=$e.AutomationId}
}
@{pid=$p.Id;created=$created;hwnd=$h.ToInt64();width=$width;height=$height;left=$rect.Left;top=$rect.Top;focus=$focus;controls=$controls;action=$req.action}|ConvertTo-Json -Depth 5 -Compress
