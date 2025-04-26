
#TAG=${1:-latest}
$tag = "latest"
$mount = "$((pwd).Path):C:\tmp"
docker run --rm -it -v "$mount" buildtools:$tag
