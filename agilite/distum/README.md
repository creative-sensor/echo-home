# DISTUM
Distributed Datum        This is ```RAID-10XL```!

### OBJECTIVE
- Multi-version
- Fault-tolerant
- Flat
- Granular accessible
- Loose coupling
- Personal affordable

### STORAGE STRUCTURE
```YAML
# Encrypted | Location_X
DISK_FILE_SYSTEM_UUID_A:
    - ./[FOLDER]
    
    - ./[FILE]
    
    - ./.replicas/[FS_UUID_B]/[YYYY-MM-DD]:
        - ./[FS_UUID_B:SUB_TREE]/*
    
    - ./.replicas/[FS_LABEL_B]/[YYYY-MM-DD]:
        - ./[FS_LABEL_B:SUB_TREE]/*
    
    - ./.replicas/[FS_UUID_C]/[YYYY-MM-DD]:
        - ./[FS_UUID_C:SUB_TREE]/*
    
    - ./.replicas/[FS_LABEL_C]/[YYYY-MM-DD]:
        - ./[FS_LABEL_C:SUB_TREE]/*



# Encrypted | Location_Y
DISK_FILE_SYSTEM_UUID_B:
    - ./[FOLDER]
    
    - ./[FILE]
    
    - ./.replicas/[FS_UUID_C]/[YYYY-MM-DD]:
        - ./[FS_UUID_C:SUB_TREE]/*
    
    - ./.replicas/[FS_LABEL_C]/[YYYY-MM-DD]:
        - ./[FS_LABEL_C:SUB_TREE]/*
    
    - ./.replicas/[FS_UUID_A]/[YYYY-MM-DD]:
        - ./[FS_UUID_A:SUB_TREE]/*
    
    - ./.replicas/[FS_LABEL_A]/[YYYY-MM-DD]:
        - ./[FS_LABEL_A:SUB_TREE]/*



# Encrypted | Location_Z
DISK_FILE_SYSTEM_UUID_C:
    - ./[FOLDER]
    
    - ./[FILE]
    
    - ./.replicas/[FS_UUID_A]/[YYYY-MM-DD]:
        - ./[FS_UUID_A:SUB_TREE]/*
    
    - ./.replicas/[FS_LABEL_A]/[YYYY-MM-DD]:
        - ./[FS_LABEL_A:SUB_TREE]/*
    
    - ./.replicas/[FS_UUID_B]/[YYYY-MM-DD]:
        - ./[FS_UUID_B:SUB_TREE]/*
    
    - ./.replicas/[FS_LABEL_B]/[YYYY-MM-DD]:
        - ./[FS_LABEL_B:SUB_TREE]/*
```


### DATA ANALYTICS
```bash
cd ${MOUNTPOINT_UUID_A}
find .  >   ${DATUM_}/fs-[UUID_A]

cd ${MOUNTPOINT_UUID_B}
find .  >  ${DATUM_}/fs-[UUID_B]

cd ${MOUNTPOINT_UUID_C}
find .  >  ${DATUM_}/fs-[UUID_C]
```


### INCREMENTAL WRITE
```YAML
DISK_FILE_SYSTEM_UUID_ICOW:
    - ./[FOLDER]:
        - ./[YYYY-01-20]-cat:
            - ./kitty
            - ./tom
        - ./[YYYY-03-07]-ibex:
            - ./mountain
            - ./climber
        - ./[YYYY-03-25]-dog:
            - ./puppy
            - ./hunter
        - ./[YYYY-04-13]-lion:
            - ./king
            - ./hairy-cat
        - ./[YYYY-05-18]-dog-bites-cat:
            - ./food
            - ./sleeping
        - ./[YYYY-07-21]-lion-dies:
            - ./shot
            - ./fight
        - ./[YYYY-09-01]-tiger-leads:
            - ./arrival
            - ./stripe
        - ./[YYYY-12-31]-man-watches:
            - ./think
            - ./stupid
```

