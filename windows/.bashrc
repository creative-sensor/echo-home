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

# ---- ALIAS ----
#alias vim='vimx'
alias git-glog='git log --all --decorate --oneline --graph'
alias gitroot='cd $(git rev-parse --show-toplevel)'
alias grin='grep -rin '
alias ll='ls -la '

# ---- VARSET ----
export LOCAL_BIN=~/.local/bin
export PYTHONPATH=~/AppData/Local/Programs/Python/Python310
export GVIMPATH='/c/Program Files (x86)/Vim/vim90'
PATH=$PATH:$LOCAL_BIN:$PYTHONPATH:$GVIMPATH

COLOR1='\[\033[38;5;34m\]'
COLOR2='\[\033[38;5;161m\]'
BOLD='\[\033[1m\]' # Work only if "allow bold text" setting is enabled
STOP='\[\033[0m\]'

PS1="[${BOLD}${COLOR1}\u${STOP}@${BOLD}${COLOR2}\h \W${STOP}]\$ "
PROMPT_COMMAND='echo -ne "\033]0;$(basename $PWD)\007"'


# ---- EDIT ----
source ~/.bashrc.edit
