prefix=$1
sudo mkdir -p  /data/${prefix}/{0,1,2}
sudo chgrp -R microk8s /data/${prefix}
sudo chmod -R 770 /data/${prefix}

