# .bashrc

# Source global definitions
if [ -f /etc/bashrc ]; then
	. /etc/bashrc
fi

# Uncomment the following line if you don't like systemctl's auto-paging feature:
# export SYSTEMD_PAGER=

# User specific aliases and functions

# ---- FUNCTION ----
function terminame {
    # set terminal name
    echo -ne "\033]0;${1}\007"
}

function cdx {
    # teleport to directory
    local DEST=$1
    local CACHE=$HOME/.local/cdx_cache
    test -z "$DEST"  &&  cat $CACHE | sort  &&  read -p 'DEST ?= ' DEST
    count=$(grep -c -i "$DEST.*$" $CACHE)
    [[ $count -gt 1 ]]  &&  grep -i "$DEST.*$" $CACHE  &&  read -p 'DEST ?= ' DEST  &&  cd $DEST
    [[ $count -eq 1 ]]  &&  DEST=$(grep -i "$DEST.*$" $CACHE)  &&  cd $DEST
    [[ $count -eq 0 ]]  &&  cd $DEST && echo $(pwd) >> $CACHE
}

function git_root {
    GIT_ROOT=$(git rev-parse --show-toplevel 2> /dev/null | awk -F ":" '{ $1="/"$1 ; print $1$2}') &&
        export GIT_ROOT && echo $GIT_ROOT && return
    echo "NOT_A_GIT_ROOT"
}
export -f git_root

function ffunction {
    local NAME=$1
    shift
    if test -x .function/$NAME ; then
        .function/$NAME $@
    else
        $(git_root)/codev/commonwealth/interface/$NAME $@
    fi
}
# ---- ALIAS ----
#alias vim='vimx'
alias git-glog='git log --all --decorate --oneline --graph'
alias gitroot='cd $(git rev-parse --show-toplevel)'
alias grin='grep -rin '
alias ll='ls -la '

# ---- VARSET ----
declare -A DICT
export LOCAL_BIN=~/.local/bin
export PYTHONPATH=~/AppData/Local/Programs/Python/Python310
export PYTHONPATH_SCRIPT=$PYTHONPATH/Scripts
export GVIMPATH='/c/Program Files (x86)/Vim/vim90'
NODEPATH=$LOCAL_BIN/node-v18.13.0-win-x64
GITBASH_PATH=/mingw64/bin:/usr/local/bin:/usr/bin:/bin:/mingw64/bin:/usr/bin/vendor_perl:/usr/bin/core_perl:/c/Windows/system32:/c/Windows:/c/Windows/System32/Wbem:/c/Windows/System32/WindowsPowerShell/v1.0:/c/Windows/System32/OpenSSH
DOCKER_DESKTOP='/c/Program Files/Docker/Docker/resources/bin:/c/ProgramData/DockerDesktop/version-bin'
PATH=$LOCAL_BIN:$NODEPATH:$PYTHONPATH:$PYTHONPATH_SCRIPT:$GITBASH_PATH:$GVIMPATH:$DOCKER_DESKTOP

COLOR1='\[\033[38;5;135m\]'
COLOR2='\[\033[38;5;161m\]'
BOLD='\[\033[1m\]' # Work only if "allow bold text" setting is enabled
STOP='\[\033[0m\]'

PS1="[${BOLD}${COLOR1}\u${STOP}@${BOLD}${COLOR2}\h \W${STOP}]\$ "
PROMPT_COMMAND='echo -ne "\033]0;$(basename $PWD)\007"'


# ---- EDIT ----
source ~/.bashrc.edit
