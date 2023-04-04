# .bashrc

# Source global definitions
if [ -f /etc/bashrc ]; then
	. /etc/bashrc
fi

# Uncomment the following line if you don't like systemctl's auto-paging feature:
# export SYSTEMD_PAGER=

# User specific aliases and functions

# ---- FUNCTION ----
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
    GIT_ROOT=$(git rev-parse --show-toplevel 2> /dev/null) &&
        export GIT_ROOT=$GIT_ROOT &&
        echo $GIT_ROOT && return
    echo "NOT_A_GIT_ROOT"
}
export -f git_root

function ffunction {
    local NAME=$1; shift
    if test -x .function/$NAME ; then .function/$NAME $@
    else
        echo "ffunction: calling default" 1>&2
        $(git_root)/codev/commonwealth/interface/$NAME $@
    fi
}
export -f ffunction

# ---- ALIAS ----
alias vim='vimx'
alias vim-py='vimx -u ~/.vim/rc/python'
alias git-glog='git log --all --decorate --oneline --graph'
alias gitroot='cd $(git rev-parse --show-toplevel)'
alias grin='grep -rin '
alias date-epoch='date +%Y-%m-%d_%s'


# ---- VARSET ----
export LOCAL_BIN=~/.local/bin
PATH=$PATH:$LOCAL_BIN

COLOR1='\[\033[38;5;34m\]'
COLOR2='\[\033[38;5;161m\]'
BOLD='\[\033[1m\]' # Work only if "allow bold text" setting is enabled
STOP='\[\033[0m\]'

PS1="[${BOLD}${COLOR1}\u${STOP}@${BOLD}${COLOR2}\h \W${STOP}]\$ "
PROMPT_COMMAND='echo -ne "\033]0;$(basename $PWD)\007"'

#export MOZILLA_FIREFOX_PROFILE=
#export MOZILLA_THUNDERBIRD_PROFILE=
#export GNUPGHOME=

# ---- EDIT ----
test -r ~/.bashrc.edit && source ~/.bashrc.edit
