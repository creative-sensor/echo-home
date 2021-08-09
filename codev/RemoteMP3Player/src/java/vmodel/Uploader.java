/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package vmodel;

import entity.Mp3item;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.naming.Context;
import javax.naming.InitialContext;
import javax.naming.NamingException;
import db.facades.Mp3itemFacadeLocal;

/**
 *
 * @author creativ
 */
public class Uploader {
    Mp3itemFacadeLocal mp3itemFacade = lookupMp3itemFacadeLocal();

    private Mp3itemFacadeLocal lookupMp3itemFacadeLocal() {
        try {
            Context c = new InitialContext();
            return (Mp3itemFacadeLocal) c.lookup("java:global/RemoteMP3Player/Mp3itemFacade!session.Mp3itemFacadeLocal");
        } catch (NamingException ne) {
            Logger.getLogger(getClass().getName()).log(Level.SEVERE, "exception caught", ne);
            throw new RuntimeException(ne);
        }
    }

    public Uploader() {
        Mp3item newItem = new Mp3item();
        newItem.setArtist("duy quang");
        newItem.setName("trang tan tren he pho");
        newItem.setLyrics("toi gap lai anh");
        
        File f = new File("/home/creativ/Downloads/ca_colle.mp3");
        byte[] binstr = new byte[(int)f.length()];
        try {
            new FileInputStream(f).read(binstr);
        } catch (FileNotFoundException ex) {
            Logger.getLogger(Uploader.class.getName()).log(Level.SEVERE, null, ex);
        } catch (IOException ex) {
            Logger.getLogger(Uploader.class.getName()).log(Level.SEVERE, null, ex);
        }
        
        
        mp3itemFacade.create(newItem);
    }
    
    
    
}
