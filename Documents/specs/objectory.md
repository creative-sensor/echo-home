# DEFINITION

An `objectory` is the file system directory which is organized in object-oriented style.
The directory acts like a callable object (function invoking and property reading).

# STRUCTURE

```
<FOLDER_NAME>/.properd
<FOLDER_NAME>/.properd/meta.yaml
<FOLDER_NAME>/.properd/VARSET
<FOLDER_NAME>/.function
<FOLDER_NAME>/.function/start
<FOLDER_NAME>/.template
<FOLDER_NAME>/datum
```

- `.properd`: contains file which represent properties of the object folder
- `.properd/meta.yaml`: has content of a uuid value
- `.properd/VARSET`: contains common environment variables as settings
- `.function`: contains script which handle function of the object folder
- `.function/start`: where to get started with the object if user has no idea about current functions
- `.template`: optional, if template is defined, its generated output should place file whose name copies original template name inside objectory 
- `datum`: optional, contains data and does not belong to git repo


# USAGE

To call function or read properd, current working directory should be path to objectory itself

Example:
```sh
cd ${PATH_TO_OBJECTORY}
.function/start
```


# INSTANCE

yaml format is used to illustrate folder structure and its content

```yaml
OBJ_DIR:
    dataA: "I9hgd77/HGw="
    dataB: "mEH0fifLg+c="
    dataC: "51I/pErIeN0="

    .function:
        read: "python"
        write: "bash"
        search: "QL"
        sort: "index"

    .template:
        config: ["yaml","json","toml]
        pattern: "regex"

    .properd:
        gedrive.yaml:
            bundle: true
            encryption: true
        node.yaml: "---"
        meta.yaml: "+=$"
        attrs.json: "x:82"
        links.yaml: "eyc2 --> pq7s"
```
