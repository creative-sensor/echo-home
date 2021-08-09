/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package db.facades;

import entity.Mp3item;
import java.util.List;
import javax.ejb.Local;

/**
 *
 * @author creativ
 */
@Local
public interface Mp3itemFacadeLocal {

    void create(Mp3item mp3item);

    void edit(Mp3item mp3item);

    void remove(Mp3item mp3item);

    Mp3item find(Object id);

    List<Mp3item> findAll();

    List<Mp3item> findRange(int[] range);

    int count();
    
}
