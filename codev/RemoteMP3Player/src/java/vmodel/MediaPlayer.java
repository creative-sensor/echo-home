/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package vmodel;

import db.facades.PlaylistFacadeLocal;
import device.FlashPlayer;
import entity.Mp3item;
import entity.Playlist;
import java.util.Collection;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.naming.Context;
import javax.naming.InitialContext;
import javax.naming.NamingException;
import org.zkoss.bind.annotation.BindingParam;
import org.zkoss.bind.annotation.Command;
import org.zkoss.bind.annotation.GlobalCommand;
import org.zkoss.bind.annotation.NotifyChange;
import org.zkoss.zk.ui.Executions;

/**
 *
 * @author creativ
 */
public class MediaPlayer {
    PlaylistFacadeLocal playlistFacade = lookupPlaylistFacadeLocal();
    FlashPlayer mp3Decoder = new FlashPlayer(FlashPlayer.MODE_MULTI);
    String playerHTML = mp3Decoder.getDefaultHTML();
    
    Collection<Playlist> playlists = playlistFacade.findAll();

    @GlobalCommand
    @NotifyChange({"playerHTML","playlists"})
    public void update(){
        this.playlists = playlistFacade.findAll();
        //this.playerHTML = mp3Decoder.getDefaultHTML();
    }
    
    @Command
    @NotifyChange("playerHTML")
    public void play(@BindingParam("record") Playlist p){
        String remoteIP = Executions.getCurrent().getRemoteAddr();
        //remoteIP.matches();
        this.mp3Decoder.setPlayList((List<Mp3item>) p.getMp3itemCollection());
        this.playerHTML = mp3Decoder.getHTML();
        
    }
    public Collection<Playlist> getPlaylists() {
        return playlists;
    }

    
    public String getPlayerHTML() {
        return playerHTML;
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
}
