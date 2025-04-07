# escape=`

FROM mcr.microsoft.com/windows:ltsc2019-amd64

SHELL ["cmd", "/S", "/C"]

ADD https://aka.ms/vs/17/release/vs_buildtools.exe  vs_buildtools.exe
ADD .vsconfig  .vsconfig
RUN start /w vs_buildtools.exe --quiet --wait --norestart --nocache install `
        --installPath "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools" `
		--add Microsoft.VisualStudio.Component.Roslyn.Compiler `
		--add Microsoft.Component.MSBuild `
		--add Microsoft.VisualStudio.Component.CoreBuildTools `
		--add Microsoft.VisualStudio.Workload.MSBuildTools `
		--add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    	--add Microsoft.VisualStudio.Component.Windows10SDK.20348 `
		--add Microsoft.VisualStudio.ComponentGroup.VC.Tools.142.x86.x64


ENTRYPOINT ["C:\\Program Files (x86)\\Microsoft Visual Studio\\2022\\BuildTools\\Common7\\Tools\\VsDevCmd.bat", "&&", "powershell.exe", "-NoLogo", "-ExecutionPolicy", "Bypass"]
#ENTRYPOINT ["powershell.exe", "-NoLogo", "-ExecutionPolicy", "Bypass"]
