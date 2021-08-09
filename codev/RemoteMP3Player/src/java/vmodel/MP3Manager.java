/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package vmodel;

import device.Config;
import device.FlashPlayer;
import entity.Mp3item;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.FileReader;
import java.io.IOException;
import java.util.Arrays;
import java.util.Iterator;
import java.util.LinkedList;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.naming.Context;
import javax.naming.InitialContext;
import javax.naming.NamingException;
import org.apache.commons.io.FileUtils;
import org.apache.commons.io.output.ByteArrayOutputStream;
import org.jaudiotagger.audio.AudioFile;
import org.jaudiotagger.audio.AudioFileIO;
import org.jaudiotagger.audio.exceptions.CannotReadException;
import org.jaudiotagger.audio.exceptions.InvalidAudioFrameException;
import org.jaudiotagger.audio.exceptions.ReadOnlyFileException;
import org.jaudiotagger.tag.FieldKey;
import org.jaudiotagger.tag.TagException;
import org.zkoss.bind.annotation.BindingParam;
import org.zkoss.bind.annotation.Command;
import org.zkoss.bind.annotation.NotifyChange;
import org.zkoss.util.media.Media;
import db.facades.Mp3itemFacadeLocal;
import db.exfacades.Mp3itemFacadeLocalExt;
import java.io.InputStream;
import java.net.MalformedURLException;
import java.net.URL;
import java.util.ArrayList;
import org.zkoss.zk.ui.Execution;
import org.zkoss.zk.ui.Executions;

/**
 *
 * @author creativ
 */
public class MP3Manager {

    Mp3itemFacadeLocalExt mp3itemFacadeExt = lookupMp3itemFacadeExtLocal();
    String rootpath = Config.rootpath;
    List<Mp3item> mp3List = new ArrayList<Mp3item>();
    Mp3item selectedMp3 = new Mp3item();
    /*
     * Index of current section in root directory
     */
    int sectionCursor;
    FlashPlayer fplayer = new FlashPlayer(0);
    String playerHTML;

    public MP3Manager() {
    }

    @Command
    @NotifyChange("mp3List")
    public void saveMetaData() {
        mp3itemFacadeExt.edit(selectedMp3);
    }

    @Command
    @NotifyChange("mp3List")
    public void importURL() {
        int i = 0;
    }

    // insert a new record for new saved file
    private Mp3item record(File file) {
        Mp3item mp3item = new Mp3item();
        try {
            String artist = null;
            String title = null;
            AudioFile mp3 = AudioFileIO.read(file);
            org.jaudiotagger.tag.Tag tag = mp3.getTag();

            if (tag != null) {
                artist = tag.getFirst(FieldKey.ARTIST);
                title = tag.getFirst(FieldKey.TITLE);
            }


            mp3item.setArtist(artist);
            mp3item.setName(title);


        } catch (CannotReadException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        } catch (IOException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        } catch (TagException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        } catch (ReadOnlyFileException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        } catch (InvalidAudioFrameException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        }

        mp3item.setPath(file.getPath().replace(rootpath, ""));

        this.mp3itemFacadeExt.create(mp3item);


        return mp3item;
    }

    private File saveFile(InputStream dataStream, String filename) {


        // GET THE LATEST USED SECTION
        try {

            BufferedReader bufferedReader = new BufferedReader(new FileReader(rootpath + ".section.cursor"));
            sectionCursor = Integer.parseInt(bufferedReader.readLine());

        } catch (FileNotFoundException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        } catch (IOException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        }


        // INCREMENT SECTION CURSOR IF NECESSARY
        if (new File(rootpath + sectionCursor).list().length >= Config.sectionSize) {
            sectionCursor++;
            try {
                FileUtils.forceMkdir(new File(rootpath + sectionCursor));
                FileUtils.writeStringToFile(new File(rootpath + ".section.cursor"), Integer.toString(sectionCursor));
            } catch (IOException ex) {
                Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
            }
        }


        // STORE FILES INTO FILESYSTEM
        String savePath = sectionCursor + "/" + filename;
        FileOutputStream fileOutputStream = null;
        File file = new File(rootpath + savePath);
        int i = 1;
        while (file.exists()) {
            file = new File(rootpath + savePath + "." + i + ".mp3");
            i++;
        }
        try {
            fileOutputStream = new FileOutputStream(file);
            ByteArrayOutputStream arrayOutputStream = new ByteArrayOutputStream();
            arrayOutputStream.write(dataStream);
            arrayOutputStream.writeTo(fileOutputStream);
        } catch (FileNotFoundException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        } catch (IOException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        } finally {
            try {
                fileOutputStream.close();
            } catch (IOException ex) {
                Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
            }
        }

        return file;
    }

    @Command
    @NotifyChange("mp3List")
    public void deleteMp3() {
        // delete from file system
        File item = new File(Config.rootpath + selectedMp3.getPath());
        item.delete();

        // delete from view
        this.mp3List.remove(selectedMp3);

        // delete from database;
        this.mp3itemFacadeExt.remove(selectedMp3);
    }
    /*
     * extract id3tag from the mp3 file which has already been stored
     * on the server and save metadata;
     */

    @Command
    @NotifyChange({"selectedMp3", "playerHTML"})
    public void selectMp3(@BindingParam("item") Mp3item item) {
        this.selectedMp3 = item;

        configurePlayer(selectedMp3);
    }

    private void configurePlayer(Mp3item item) {
        List<Mp3item> pl = new LinkedList<Mp3item>();
        pl.add(item);
        this.fplayer.setPlayList(pl);

        this.playerHTML = fplayer.getHTML();
    }

    @Command
    @NotifyChange("mp3List")
    public void importMP3(@BindingParam("upload") Media[] uploadedMedia) {
        List<Media> mediaList = Arrays.asList(uploadedMedia);
        Iterator<Media> iter = mediaList.iterator();

        while (iter.hasNext()) {
            Media upload = iter.next();
            File file = saveFile(upload.getStreamData(), upload.getName());
            Mp3item newItem = record(file);
            this.mp3List.add(newItem);
        }
    }

    @Command
    @NotifyChange("mp3List")
    public void importMP3fromURL(@BindingParam("urlstr") String url) {

        File savedfile;
        URL locator;
        try {
            // locate resource
            locator = new URL(url);
            InputStream streamFromURL = locator.openStream();

            String filename = locator.getFile();
            String[] pieces = filename.split("/");
            filename = pieces[pieces.length - 1].replace("%20", " ");
            filename = filename.replace("%5B", "[");
            filename = filename.replace("%5D", "]");
            savedfile = saveFile(streamFromURL, filename);
            Mp3item item = record(savedfile);
            this.mp3List.add(item);

        } catch (MalformedURLException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        } catch (IOException ex) {
            Logger.getLogger(MP3Manager.class.getName()).log(Level.SEVERE, null, ex);
        }
    }

    @Command
    @NotifyChange("mp3List")
    public void search(@BindingParam("keyword") String keyword) {
        this.mp3List.clear();

        if (keyword.isEmpty()) {
            this.mp3List.addAll(this.mp3itemFacadeExt.findAll());
        } else {
            this.mp3List.addAll(this.mp3itemFacadeExt.search(keyword));
        }
    }

    public List<Mp3item> getMp3List() {
        return this.mp3List;
    }

    public Mp3item getSelectedMp3() {
        return this.selectedMp3;
    }

    public String getPlayerHTML() {
        return this.playerHTML;
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

    @Command
    public void gotoAdmin() throws IOException {
    }
}
