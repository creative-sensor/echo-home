$DISTRO = "Ubuntu-20.04"
$WSL_VERSION = 2
wsl --install -d $DISTRO
wsl --set-default-version $WSL_VERSION
wsl --set-default $DISTRO
wsl --set-version $DISTRO $WSL_VERSION
