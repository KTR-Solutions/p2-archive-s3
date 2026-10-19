# Loads the Excellent P2 assemblies once and serves ReadEncodedFile / WriteEncodedFile.
# Windows PowerShell 5.1 cannot load this .NET 8 app. Run with pwsh.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Emit([hashtable]$Object) {
    $json = $Object | ConvertTo-Json -Compress -Depth 5
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}

$app = $env:ARCHIVE_APP_DIR
if (-not $app) {
    Emit @{ ok = $false; error = "ARCHIVE_APP_DIR is not set" }
    exit 1
}

Set-Location $app
[Environment]::CurrentDirectory = $app

Add-Type -TypeDefinition @"
using System;
using System.Reflection;
using System.Runtime.Loader;
public sealed class AppAlc : AssemblyLoadContext {
    private readonly AssemblyDependencyResolver _resolver;
    public AppAlc(string mainAssembly) : base(isCollectible: false) {
        _resolver = new AssemblyDependencyResolver(mainAssembly);
    }
    protected override Assembly Load(AssemblyName name) {
        string path = _resolver.ResolveAssemblyToPath(name);
        return path != null ? LoadFromAssemblyPath(path) : null;
    }
    protected override IntPtr LoadUnmanagedDll(string name) {
        string path = _resolver.ResolveUnmanagedDllToPath(name);
        return path != null ? LoadUnmanagedDllFromPath(path) : IntPtr.Zero;
    }
}
"@

try {
    $alc = [AppAlc]::new((Join-Path $app "DMS.dll"))
    $asm = $alc.LoadFromAssemblyPath((Join-Path $app "Sph.Imhotep.Controller.General.dll"))
    $controller = $asm.GetType("Sph.Imhotep.Controller.General.Allgemein.ShareFolderController", $true)
    $read = $controller.GetMethod("ReadEncodedFile", [type[]]@([string]))
    $write = $controller.GetMethod("WriteEncodedFile", [type[]]@([string], [byte[]]))
    if ($null -eq $read -or $null -eq $write) {
        throw "ReadEncodedFile or WriteEncodedFile was not found"
    }
} catch {
    Emit @{ ok = $false; error = $_.Exception.Message }
    exit 1
}

[Console]::Error.WriteLine("P2 archive worker ready")
Emit @{ ok = $true; ready = $true }

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ($line.Trim().Length -eq 0) { continue }
    try {
        $request = $line | ConvertFrom-Json
        if ($request.op -eq "read") {
            $bytes = $read.Invoke($null, @([string]$request.path))
            if ($null -eq $bytes) { throw "ReadEncodedFile returned no bytes" }
            [System.IO.File]::WriteAllBytes([string]$request.file, $bytes)
            Emit @{ ok = $true }
        } elseif ($request.op -eq "write") {
            $bytes = [System.IO.File]::ReadAllBytes([string]$request.file)
            $result = $write.Invoke($null, @([string]$request.guid, [byte[]]$bytes))
            if ($result -eq $false) { throw "WriteEncodedFile returned false" }
            Emit @{ ok = $true }
        } else {
            Emit @{ ok = $false; error = "Unknown operation" }
        }
    } catch {
        Emit @{ ok = $false; error = $_.Exception.Message }
    }
}
