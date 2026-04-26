# DEFINITION

- `artefact-get-ready` is private artifact management system
- artifact includes remote hosted software packages where control is not a personal option
- To obtain an offline copy to be isolated from unexpected events such as network connection, automatic updates, outdated package, tampered-with, subscription

# STRUCTURE

### Objectory

- see spec of `objectory.md`

### Inventory

- see spec of `yson.md`
- `.properd/artefact.sha256sum` to hold checksum of packages obtained by geti
- `.properd/artefacts.yaml` to hold metadata:
```
$name: [ $version , $url_to_reach_remote_site, {"extended_": "_subkey}]"
``` 


### Function interface

###### .function/artefact-geti

- curl to save file in default storage folder:`artefacts/$name-$version`
- `artefacts/.function/geti/$name` to override 

###### .function/artefact-readi: 

- default by calling native installer/deployment system
- `artefacts/.function/readi/$name` to override

###### .function/artefacts

- Run checksum of all items in `artefacts` with `.properd/artefact.sha256sum`
- Auto-append hash entry for new $name found

