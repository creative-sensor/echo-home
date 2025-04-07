# toolset : build/compile
--------------------------------
### 0 DESCRIPTION
To provision facility for building, compiling source code in windows
- msvc
- make
- cmake
- m4

### 1 SOLUTION


### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL

###### 4.1

- Dockerfile:
```

FROM mcr.microsoft.com/windows:ltsc2019-amd64

SHELL ["cmd", "/S", "/C"]

RUN `
    # Download the Build Tools bootstrapper.
    curl -SL --output vs_buildtools.exe https://aka.ms/vs/17/release/vs_buildtools.exe `
    `
    # Install Build Tools with the Microsoft.VisualStudio.Workload.AzureBuildTools workload, excluding workloads and components with known issues.
    && (start /w vs_buildtools.exe --quiet --wait --norestart --nocache `
        --installPath "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools" `
        --add Microsoft.VisualStudio.Workload.AzureBuildTools `
        --remove Microsoft.VisualStudio.Component.Windows10SDK.10240 `
        --remove Microsoft.VisualStudio.Component.Windows10SDK.10586 `
        --remove Microsoft.VisualStudio.Component.Windows10SDK.14393 `
        --remove Microsoft.VisualStudio.Component.Windows81SDK `
        || IF "%ERRORLEVEL%"=="3010" EXIT 0) `
    `
    # Cleanup
    && del /q vs_buildtools.exe

# Define the entry point for the docker container.
# This entry point starts the developer command prompt and launches the PowerShell shell.
ENTRYPOINT ["C:\\Program Files (x86)\\Microsoft Visual Studio\\2022\\BuildTools\\Common7\\Tools\\VsDevCmd.bat", "-arch=amd64", "&&", "powershell.exe", "-NoLogo", "-ExecutionPolicy", "Bypass"]
```

- build:
```
docker build -t buildtools:latest  .
```

- DONE: need faster network download speed as large image 

- search files: ```  Get-ChildItem  -Recurse -Path  '.\'  -Filter vcvarsall.bat ```


###### 4.0 

- add/config library path will mess up the host, container-based recommended

--------------------------------
```json
{ "project_code": "MDOS" , "links": "" , "location": "" , "fpoint": "" }
```
