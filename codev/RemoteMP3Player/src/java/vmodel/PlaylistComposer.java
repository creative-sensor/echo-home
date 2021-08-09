/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package vmodel;

import db.exfacades.Mp3itemFacadeLocalExt;
import db.facades.PlaylistFacadeLocal;
import entity.Mp3item;
import entity.Playlist;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Iterator;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.naming.Context;
import javax.naming.InitialContext;
import javax.naming.NamingException;
import javax.servlet.http.HttpSession;
import org.zkoss.bind.annotation.BindingParam;
import org.zkoss.bind.annotation.Command;
import org.zkoss.bind.annotation.NotifyChange;
import org.zkoss.util.media.Media;
import org.zkoss.web.servlet.Servlets;
import org.zkoss.zk.ui.Executions;
import org.zkoss.zk.ui.Sessions;
import org.zkoss.zul.Filedownload;
import org.zkoss.zul.Messagebox;

/**
 *
 * @author creativ
 */
public class PlaylistComposer {

    Mp3itemFacadeLocalExt mp3itemFacadeExt = lookupMp3itemFacadeExtLocal();
    PlaylistFacadeLocal playlistFacade = lookupPlaylistFacadeLocal();
    Mp3itemFacadeLocalExt mp3itemFacade = lookupMp3itemFacadeExtLocal();
    // members of selected playlist
    Collection<Mp3item> memberList = new ArrayList<Mp3item>();
    List<Playlist> playlists = playlistFacade.findAll();
    Playlist selectedPlaylist;
    // result list
    Collection<Mp3item> resultList = new ArrayList<Mp3item>();

    public Collection<Mp3item> getResultList() {
        return resultList;
    }

    @Command
    @NotifyChange("resultList")
    public void search(@BindingParam("keyword") String keyword) {
        this.resultList.clear();
        this.resultList.addAll(this.mp3itemFacadeExt.search(keyword));
    }

    @Command
    @NotifyChange("memberList")
    public void dropItem(@BindingParam("item") Mp3item item) {
        if (this.selectedPlaylist == null) {
            Messagebox.show("Choose a playlist to add this item");
            return;
        }
        if (this.memberList.contains(item)) {
            Messagebox.show("this item already exists in playlist");
        } else {
            this.memberList.add(item);
            this.selectedPlaylist.setMp3itemCollection(memberList);
            this.playlistFacade.edit(selectedPlaylist);

        }
    }

    @Command
    @NotifyChange("memberList")
    public void deleteMemberItem(@BindingParam("item") Mp3item item) {
        this.memberList.remove(item);
        this.selectedPlaylist.setMp3itemCollection(memberList);
        this.playlistFacade.edit(selectedPlaylist);
    }

    public Playlist getSelectedPlaylist() {
        return selectedPlaylist;
    }

    public void setSelectedPlaylist(Playlist selectedPlaylist) {
        this.selectedPlaylist = selectedPlaylist;
    }

    @Command
    @NotifyChange({"playlists","memberList"})
    public void deletePlayList(){
        this.playlists.remove(selectedPlaylist);
        this.memberList.clear();
        this.playlistFacade.remove(selectedPlaylist);
        
    }
    @Command
    @NotifyChange({"playlists", "selectedPlaylist","memberList"})
    public void addNewPlayList(@BindingParam("name") String name) {
        if (name == null) {
            Messagebox.show("Playlist must have a name");
            return;
        }
        Playlist newPlaylist = new Playlist();
        newPlaylist.setName(name);

        this.playlists.add(newPlaylist);
        this.selectedPlaylist = newPlaylist;
        this.memberList.clear();
        this.playlistFacade.create(newPlaylist);
    }

    @Command
    public void export(@BindingParam("prefixEnabled") boolean prefixEnabled, @BindingParam("prefixValue") String pf){
        
        String prefix = "";
        int track = 1;
        
        if(prefixEnabled){
            prefix = pf;
        }
        
        String playlist = "#EXTM3U\n" + "#" + this.selectedPlaylist.getName() + "\n";
        Iterator<Mp3item> iter = this.selectedPlaylist.getMp3itemCollection().iterator();
        while(iter.hasNext()){
            Mp3item item = iter.next();
            playlist = playlist.concat("#EXTINF:" + track + ","+ item.getArtist() + " - " + item.getName()) + "\n";
            playlist = playlist.concat(prefix + item.getPath() + "\n");
            track++;
        }
        
        Filedownload.save(playlist, "text/m3u", this.selectedPlaylist.getName());
        
        
    }
    @Command
    @NotifyChange("memberList")
    public void listMembers(@BindingParam("playlist") Playlist playlist) {
        this.memberList = playlist.getMp3itemCollection();
    }

    public Collection<Mp3item> getMemberList() {
        return memberList;
    }

    public List<Playlist> getPlaylists() {
        return playlists;
    }

    private PlaylistFacadeLocal lookupPlaylistFacadeLocal() {
        try {
            Context c = new InitialContext();
            return (PlaylistFacadeLocal) c.lookup("java:global/RemoteMP3Player/PlaylistFacade!db.facades.PlaylistFacadeLocal");
        } catch (NamingException ne) {
            Logger.getLogger(getClass().getName()).log(Level.SEVERE, "exception caught", ne);
            throw new RuntimeException(ne);
        }
    }

    private Mp3itemFacadeLocalExt lookupMp3itemFacadeExtLocal() {
        try {
            Context c = new InitialContext();
            return (Mp3itemFacadeLocalExt) c.lookup("java:global/RemoteMP3Player/Mp3itemFacadeExt!db.exfacades.Mp3itemFacadeLocalExt");
        } catch (NamingException ne) {
            Logger.getLogger(getClass().getName()).log(Level.SEVERE, "exception caught", ne);
            throw new RuntimeException(ne);
        }
    }
}
