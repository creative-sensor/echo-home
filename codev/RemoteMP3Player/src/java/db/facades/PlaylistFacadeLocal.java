/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package db.facades;

import entity.Playlist;
import java.util.List;
import javax.ejb.Local;

/**
 *
 * @author creativ
 */
@Local
public interface PlaylistFacadeLocal {

    void create(Playlist playlist);

    void edit(Playlist playlist);

    void remove(Playlist playlist);

    Playlist find(Object id);

    List<Playlist> findAll();

    List<Playlist> findRange(int[] range);

    int count();
    
}
