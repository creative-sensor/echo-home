
### overwrite remote tree
```bash
git push --force origin <your_branch>
```


### rebase
```bash
git featch FETCH_HEAD
git rebase mytestbranch FETCH_HEAD #rebase fetch-head on tip of mytestbranch
git rebase FETCH_HEAD mytestbranch #rebase mytestbranch on tip of fetch-head 

vim changed-file
git add changed-file
git rebase --continue
```


### git merge

Step 1. Fetch and check out the branch for this merge request
```bash
git fetch origin
git checkout -b mytestbranch origin/mytestbranch
```
Step 2. Review the changes locally

Step 3. Merge the branch and fix any conflicts that come up
```bash
git checkout master
git merge --no-ff mytestbranch
```
Step 4. Push the result of the merge to GitLab
```
git push origin master
```


### git-log-with-graph ,
```bash
git log --all --decorate --oneline --graph
```

### git-alias ,
https://git.wiki.kernel.org/index.php/Aliases#Aliases


### git-credential ,
```bash
git config credential.helper 'store'  
echo 'credential' > ~/.git-creadential
```


### git-fixup , git-commit-fixup , git-rebase-autosquash ,

#Git tree: commit A --> B --> B1 (fixup) --> B2 (fixup)

### Add fixup commit
```bash
git commit --fixup <commit_B>
```

### Squeeze those fixups into single commit B
```bash
git rebase -i --autosquash <commit_A>
```



### git-log , search-commit-of-path , 
```bash
git log -p /path/to/file
```



### delete-remote-branch , 
```bash
git push <remote>  --delete <branch>
```


### cherry-pick-commit-from-another-branch-to-master ,
```bash
git checkout master
git cherry-pick 62ecb336cbfd629
```


### print-git-root-dir ,
```bash
git rev-parse --show-toplevel
```
