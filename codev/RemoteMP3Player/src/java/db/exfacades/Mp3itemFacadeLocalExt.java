/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package db.exfacades;

import entity.Mp3item;
import java.util.List;

/**
 *
 * @author creativ
 */
public interface Mp3itemFacadeLocalExt extends db.facades.Mp3itemFacadeLocal{
    public List<Mp3item> search(String keyword);
    
}
