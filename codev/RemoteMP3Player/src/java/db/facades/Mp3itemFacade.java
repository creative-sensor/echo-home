/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package db.facades;

import entity.Mp3item;
import javax.ejb.Stateless;
import javax.persistence.EntityManager;
import javax.persistence.PersistenceContext;

/**
 *
 * @author creativ
 */
@Stateless
public class Mp3itemFacade extends AbstractFacade<Mp3item> implements Mp3itemFacadeLocal {
    @PersistenceContext(unitName = "RemoteMP3PlayerPU")
    private EntityManager em;

    @Override
    protected EntityManager getEntityManager() {
        return em;
    }

    public Mp3itemFacade() {
        super(Mp3item.class);
    }
    
}
